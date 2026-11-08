// Package authz is varTrack's one permission engine.
//
// Who may do what is decided here and nowhere else. The gateway's CLI handlers call
// the engine in-process; the orchestrator's web UI calls it through the Authz gRPC
// service (server.go). Both are enforcement points: they gather the facts, ask, and
// do what the answer says.
//
// Git is the source of truth. varTrack keeps no roles of its own: the caller's own
// token is asked of the rule's Git platform, who they are, what they may do in the
// repositories the rule covers and, for a sync, whether they may push to the branch
// that feeds the environment (branch protection included). The one list varTrack
// does keep is config.cue's ui.auth.admins ("platform:login", who may do anything).
// An optional policy (auth.opa) is the last step; it can refuse more, never grant
// more than Git does.
//
// The order of every decision:
//
//  1. identify the token on exactly one platform (never tried on all of them);
//  2. admin? an admin is allowed whatever Git says (a policy still runs);
//  3. the role in the rule's repositories: read, write or admin;
//  4. for a sync only: the branch the rule says feeds the environment, and whether
//     the caller may push to it;
//  5. the policy, for the actions it covers (sync, dry runs, validate).
package authz

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"path"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"

	"gateway-service/internal/auth"
	"gateway-service/internal/models"
	pb "vartrack/libs/gen/vartrack/v1/models"
)

// Action is what a caller wants to do.
type Action string

const (
	// Read: see a rule, an environment, a task. Needs read access to the rule's
	// repositories (to the repository an environment was synced from, for one).
	Read Action = "read"
	// DryRun: run a sync that writes nothing. Needs read access.
	DryRun Action = "dry_run"
	// Sync: write to the datasource. Needs what a direct push to the branch that
	// feeds the environment needs: a sync bypasses Git review, so it may do no more
	// than the caller could do in Git.
	Sync Action = "sync"
	// Validate: check a file. Needs read access, or just a good token when no rule
	// is named.
	Validate Action = "validate"
	// List: list tasks. Judged rule by rule with ReadableRules.
	List Action = "list"
)

// The ways in, as a policy sees them (OPAInput.Via).
const (
	ViaCLI = "cli"
	ViaUI  = "ui"
)

// DeniedBy says which step of the decision said no.
const (
	DeniedByGit    = "git"    // the platform's answer
	DeniedByPolicy = "policy" // auth.opa
	DeniedByRule   = "rule"   // nothing syncs that environment
)

// ErrNoRule: no rule syncs to the datasource asked about.
var ErrNoRule = errors.New("no rule syncs to this datasource")

// AmbiguousRuleError: rules on several platforms write to the datasource and the
// request does not name one.
type AmbiguousRuleError struct {
	Datasource string
	Platforms  []string
}

func (e *AmbiguousRuleError) Error() string {
	return "datasource " + e.Datasource + " has rules on " + strings.Join(e.Platforms, ", ") +
		"; say which with --platform"
}

// PlatformRequiredError: the request names no rule and no platform, and the bundle
// has several platforms. A token is shown to one platform only, so it must be said
// which.
type PlatformRequiredError struct{ Platforms []string }

func (e *PlatformRequiredError) Error() string {
	return "say which Git platform the token is for (--platform): " + strings.Join(e.Platforms, ", ")
}

// UnknownPlatformError: the platform named is not one of the bundle's.
type UnknownPlatformError struct {
	Platform  string
	Platforms []string
}

func (e *UnknownPlatformError) Error() string {
	return "no Git platform " + e.Platform + " in config.cue (" + strings.Join(e.Platforms, ", ") + ")"
}

// PolicyError: the policy could not be evaluated. The request is refused.
type PolicyError struct{ Err error }

func (e *PolicyError) Error() string { return "policy evaluation error: " + e.Err.Error() }
func (e *PolicyError) Unwrap() error { return e.Err }

// Identity is a token and the Git platform that issued it. Platform may be empty
// when a rule names it.
type Identity struct {
	Platform string
	Token    string
}

// Who is a caller as their platform knows them.
type Who struct {
	Platform string
	Login    string
	Admin    bool // in config.cue's ui.auth.admins
}

// Name is "login@platform", recorded with the caller's tasks.
func (w Who) Name() string { return w.Login + "@" + w.Platform }

// Rule is what the engine needs to know of a rule. The CLI's rules come from the
// gateway's bundle; the orchestrator sends the tenant's rules it reads from the
// schema repository as facts of the request.
type Rule struct {
	Platform    string
	Datasource  string
	Include     []string          // repositories: names and glob patterns
	Exclude     []string          // excluded repositories
	BranchMap   map[string]string // branch → environment
	BranchAsEnv bool
	// Branch is the rule's own branch: what feeds "default" and the file_path_map environments,
	// and what self-heal reads when no branch_map names one. "main" when empty.
	Branch       string
	FilePathEnvs []string // the environments the rule's file_path_map names
	Envs         []string // the environments to judge for "may sync at all"
	Tenants      []string // the tenants whose bundle.json may layer the rule: any when empty
}

// RuleOf is the engine's view of a bundle rule: its repository patterns with the
// enabled overrides folded in.
func RuleOf(r *pb.Rule) Rule {
	rule := Rule{
		Platform:    r.GetPlatform(),
		Datasource:  r.GetDatasource(),
		BranchMap:   r.GetBranchMap(),
		BranchAsEnv: r.GetBranchAsEnv(),
		Branch:      r.GetBranch(),
		Tenants:     r.GetTenants(),
	}
	for env := range r.GetFilePathMap() {
		rule.FilePathEnvs = append(rule.FilePathEnvs, env)
	}
	sort.Strings(rule.FilePathEnvs)
	rule.Include = append(rule.Include, r.GetRepositories()...)
	rule.Exclude = append(rule.Exclude, r.GetExcludeRepositories()...)
	for _, o := range r.GetOverrides() {
		if o.GetEnable() {
			rule.Include = append(rule.Include, o.GetMatchRepositories()...)
			rule.Exclude = append(rule.Exclude, o.GetExcludeRepositories()...)
		}
	}
	return rule
}

// Request is one question.
type Request struct {
	Identities []Identity
	Action     Action

	// The rule asked about: Rule when the caller supplies its facts, else the one in
	// the gateway's bundle for RulePlatform (may be empty when one rule writes to
	// the datasource) and RuleDatasource. All empty: the question is about the
	// caller alone (a validate that names no rule).
	Rule           *Rule
	RulePlatform   string
	RuleDatasource string

	Env        string
	SourceRepo string // the repository Env was last synced from (Read only)
	TenantID   string
	FilePath   string
	Via        string
	Resource   string // what a policy sees as the resource; "datasource" when empty

	tenantChecked bool // TenantID was already checked for this caller and rule (ReadableRules)
}

// Decision is the engine's answer.
type Decision struct {
	Allowed bool
	// Reason says why not, complete, for the caller; empty when allowed.
	Reason   string
	DeniedBy string
	Who
	// Permission is "admin", "write", "read" or "none": what the caller may do in the
	// rule's repositories.
	Permission string
	// Branch is the branch a sync was judged on: the one a re-sync must read. A dry
	// run reads the same branch, so it carries it too. Empty when no branch feeds Env.
	Branch string
	// Tenant is the tenant the caller was judged to act for: declared in config.cue, one
	// the rule allows and one the caller can reach in Git. This, not what the request
	// said, is what the grant carries.
	Tenant string
}

// RuleAccess is what one caller may do with one rule.
type RuleAccess struct {
	Index      int // position in the rules asked about
	Permission string
	// CanSync: at least one of the rule's environments may be synced by the caller.
	CanSync bool
}

// Readable is the answer to ReadableRules: only the rules the caller may read.
type Readable struct {
	Admin bool
	Rules []RuleAccess
}

// RuleSource is what the engine needs of the bundle.
type RuleSource interface {
	FindRule(platformName, datasourceName string) *pb.Rule
	RulesForDatasource(datasource string) []*pb.Rule
	Rules() []*pb.Rule
	GetPlatformForRule(ctx context.Context, platformName, datasourceName string) (models.Platform, error)
	PlatformNames() []string
}

// Engine is the one permission engine.
type Engine struct {
	rules  RuleSource
	admins map[string]bool // "platform:login", lower case
	opa    *auth.OPAEvaluator
	cache  *cache
}

// Option configures an Engine.
type Option func(*Engine)

// WithPolicy sets the optional policy that runs after Git has allowed. A nil
// evaluator means there is none.
func WithPolicy(opa *auth.OPAEvaluator) Option { return func(e *Engine) { e.opa = opa } }

// WithCacheTTL sets how long the platforms' answers are kept (DefaultTTL when
// unset); zero or less turns the cache off.
func WithCacheTTL(ttl time.Duration) Option { return func(e *Engine) { e.cache = newCache(ttl) } }

// NormalizeAdmins turns the entries of config.cue's ui.auth.admins into the
// "platform:login" the engine matches on. An entry that names no platform is that
// login on the one platform the bundle has; with several, which platform is meant
// is a mistake the operator must fix ("alice" on GitHub need not be "alice" on
// Gitea), so it is an error rather than a guess.
func NormalizeAdmins(entries, platforms []string) ([]string, error) {
	var out []string
	for _, entry := range entries {
		entry = strings.ToLower(strings.TrimSpace(entry))
		switch {
		case entry == "":
		case strings.Contains(entry, ":"):
			out = append(out, entry)
		case len(platforms) == 1:
			out = append(out, strings.ToLower(platforms[0])+":"+entry)
		default:
			return nil, fmt.Errorf("ui.auth.admins entry %q must name its platform, e.g. %s:%s",
				entry, strings.ToLower(firstOr(platforms, "gitea")), entry)
		}
	}
	return out, nil
}

func firstOr(list []string, fallback string) string {
	if len(list) > 0 {
		return list[0]
	}
	return fallback
}

// New is the engine for rules, with admins ("platform:login"; see NormalizeAdmins).
func New(rules RuleSource, admins []string, opts ...Option) *Engine {
	set := map[string]bool{}
	for _, a := range admins {
		if a = strings.ToLower(strings.TrimSpace(a)); a != "" {
			set[a] = true
		}
	}
	e := &Engine{rules: rules, admins: set, cache: newCache(DefaultTTL)}
	for _, o := range opts {
		o(e)
	}
	return e
}

// ── what a caller is ─────────────────────────────────────────────────────────

// platformFor is the one platform a token is for: the one named, else the only one
// the bundle has. A token is never tried on every platform.
func (e *Engine) platformFor(named string) (string, error) {
	names := e.rules.PlatformNames()
	if named != "" {
		for _, n := range names {
			if n == named {
				return named, nil
			}
		}
		return "", &UnknownPlatformError{Platform: named, Platforms: names}
	}
	if len(names) == 1 {
		return names[0], nil
	}
	return "", &PlatformRequiredError{Platforms: names}
}

func (e *Engine) isAdmin(platform, login string) bool {
	return e.admins[strings.ToLower(platform+":"+login)]
}

func (e *Engine) login(ctx context.Context, platform, datasource, token string) (string, error) {
	return cached(e.cache, platform, token, "login", func() (string, error) {
		p, err := e.rules.GetPlatformForRule(ctx, platform, datasource)
		if err != nil {
			return "", err
		}
		return p.CallerLogin(ctx, token)
	})
}

// Identify says who a token belongs to on its platform. It returns
// models.ErrTokenRejected when the platform does not accept it and a
// *PlatformRequiredError when the platform must be named.
func (e *Engine) Identify(ctx context.Context, id Identity) (Who, error) {
	platform, err := e.platformFor(id.Platform)
	if err != nil {
		return Who{}, err
	}
	login, err := e.login(ctx, platform, "", id.Token)
	if err != nil {
		return Who{}, err
	}
	return Who{Platform: platform, Login: login, Admin: e.isAdmin(platform, login)}, nil
}

// Forget drops what the engine remembers about the tokens.
func (e *Engine) Forget(ids []Identity) {
	for _, id := range ids {
		e.cache.forget(id.Platform, id.Token)
	}
}

// BundleRules are the rules of the gateway's own bundle.
func (e *Engine) BundleRules() []Rule {
	var out []Rule
	for _, r := range e.rules.Rules() {
		out = append(out, RuleOf(r))
	}
	return out
}

// ResolveRule is the rule of the gateway's bundle for platform (may be empty) and
// datasource. It returns ErrNoRule when none fits and an *AmbiguousRuleError when
// the platform is needed.
func (e *Engine) ResolveRule(platform, datasource string) (Rule, error) {
	if platform != "" {
		if r := e.rules.FindRule(platform, datasource); r != nil {
			return RuleOf(r), nil
		}
		return Rule{}, ErrNoRule
	}
	rules := e.rules.RulesForDatasource(datasource)
	switch len(rules) {
	case 0:
		return Rule{}, ErrNoRule
	case 1:
		return RuleOf(rules[0]), nil
	}
	names := make([]string, len(rules))
	for i, r := range rules {
		names[i] = r.GetPlatform()
	}
	return Rule{}, &AmbiguousRuleError{Datasource: datasource, Platforms: names}
}

// caller is one caller judged against one rule.
type caller struct {
	login    string
	platform string
	token    string // the token for the rule's platform; empty when they have none
	admin    bool
}

// callerFor works out who the caller is on rule's platform: which of the identities
// is for that platform, who it belongs to, and whether any of them is an admin.
func (e *Engine) callerFor(ctx context.Context, ids []Identity, rule Rule) (*caller, error) {
	if len(ids) == 0 {
		return nil, models.ErrTokenRejected
	}
	c := &caller{platform: rule.Platform}
	var others []Identity
	for _, id := range ids {
		switch {
		case id.Platform == rule.Platform || (id.Platform == "" && len(ids) == 1):
			c.token = id.Token
		default:
			others = append(others, id)
		}
	}
	if c.token != "" {
		login, err := e.login(ctx, rule.Platform, rule.Datasource, c.token)
		if err != nil {
			return nil, err
		}
		c.login = login
		c.admin = e.isAdmin(rule.Platform, login)
	}
	// An admin of any platform the caller signed in to may do anything, so the other
	// identities count, but only to find that out: a failure there is not this
	// question's failure.
	for i := 0; i < len(others) && !c.admin; i++ {
		id := others[i]
		if id.Platform == "" {
			continue
		}
		login, err := e.login(ctx, id.Platform, "", id.Token)
		if err != nil {
			slog.Debug("authz: identity on another platform not used", "platform", id.Platform, "error", err)
			continue
		}
		if e.isAdmin(id.Platform, login) {
			c.admin = true
			if c.login == "" {
				c.login = login
			}
		}
	}
	return c, nil
}

// ── what a caller may do in a rule's repositories ────────────────────────────

var fullAccess = models.RepoPermission{Read: true, Write: true, Admin: true}

// namedRepo reports whether repo is "owner/name". A repository recorded as anything
// else (a file pushed from a laptop is "local") is one no platform can vouch for.
func namedRepo(repo string) bool {
	owner, name, ok := strings.Cut(strings.Trim(repo, "/"), "/")
	return ok && owner != "" && name != "" && !strings.Contains(name, "/")
}

func (e *Engine) permission(ctx context.Context, p models.Platform, platform, token, repo string) (models.RepoPermission, error) {
	if !namedRepo(repo) {
		return models.RepoPermission{}, nil
	}
	return cached(e.cache, platform, token, "perm:"+strings.ToLower(repo), func() (models.RepoPermission, error) {
		return p.CallerPermission(ctx, token, repo)
	})
}

func (e *Engine) repos(ctx context.Context, p models.Platform, platform, token string) (map[string]models.RepoPermission, error) {
	return cached(e.cache, platform, token, "repos", func() (map[string]models.RepoPermission, error) {
		return p.CallerRepos(ctx, token)
	})
}

func (e *Engine) canPush(ctx context.Context, p models.Platform, platform, token, repo, branch string) (bool, error) {
	if !namedRepo(repo) {
		return false, nil
	}
	return cached(e.cache, platform, token, "push:"+strings.ToLower(repo)+"@"+branch, func() (bool, error) {
		return p.CallerCanPush(ctx, token, repo, branch)
	})
}

// rulePermission is what the caller may do in the repositories rule covers.
func (e *Engine) rulePermission(ctx context.Context, rule Rule, c *caller) (models.RepoPermission, error) {
	return e.rulePermissionIn(ctx, rule, c, nil)
}

// rulePermissionIn is rulePermission limited to the repositories within names (patterns): a
// tenant's. Nil means all of the rule's.
func (e *Engine) rulePermissionIn(ctx context.Context, rule Rule, c *caller, within []string) (models.RepoPermission, error) {
	if c.admin {
		return fullAccess, nil
	}
	if c.token == "" {
		return models.RepoPermission{}, nil
	}
	p, err := e.rules.GetPlatformForRule(ctx, rule.Platform, rule.Datasource)
	if err != nil {
		return models.RepoPermission{}, err
	}
	var perm models.RepoPermission
	hasGlob := false
	for _, repo := range rule.Include {
		if isGlob(repo) {
			hasGlob = true
			continue
		}
		if matchesAny(repo, rule.Exclude) || (within != nil && !matchesAny(repo, within)) {
			continue
		}
		rp, err := e.permission(ctx, p, rule.Platform, c.token, repo)
		if err != nil {
			return perm, err
		}
		if perm = perm.Or(rp); perm.Write {
			return perm, nil
		}
	}
	if !perm.Read && hasGlob {
		repos, err := e.repos(ctx, p, rule.Platform, c.token)
		if err != nil {
			return perm, err
		}
		for repo, rp := range repos {
			if matchesAny(repo, rule.Include) && !matchesAny(repo, rule.Exclude) && (within == nil || matchesAny(repo, within)) {
				perm = perm.Or(rp)
			}
		}
	}
	return perm, nil
}

// defaultEnv is the environment a push resolves to when no environment strategy names another.
const defaultEnv = "default"

// branchFor is the branch whose files land in env: the inverse of the ETL's environment
// resolver (orchestrator-service/app/pipeline/env_resolver.py), in its order.
//
//  1. a branch_map entry maps a branch to env: that branch;
//  2. env is one a file_path_map names: the rule's own branch;
//  3. branch_as_env: env itself;
//  4. env is "default", what a push resolves to when nothing else applies: the rule's own branch.
//
// "" when no branch feeds env: a pull request or tag environment, or one the rule no longer
// produces (written by an earlier version of it). The rule's own branch does not feed 2 or 4
// when branch_map sends that branch to another environment: its files land there.
// testdata/branch_for.json holds the cases; the orchestrator's tests check them against the resolver.
func branchFor(rule Rule, env string) string {
	// Deterministic when several branches map to one environment.
	var branches []string
	for branch, e := range rule.BranchMap {
		if e == env {
			branches = append(branches, branch)
		}
	}
	if len(branches) > 0 {
		sort.Strings(branches)
		return branches[0]
	}
	if slices.Contains(rule.FilePathEnvs, env) {
		return ownBranch(rule)
	}
	if rule.BranchAsEnv {
		return env
	}
	if env == defaultEnv {
		return ownBranch(rule)
	}
	return ""
}

// nothingFeeds is why an environment cannot be synced, or dry-run, when no branch feeds it.
func nothingFeeds(rule Rule, env string) string {
	return "nothing feeds " + env + " in the " + rule.Datasource + " rule: no branch_map entry maps a branch " +
		"to it, branch_as_env is off, and it is not default or a file_path_map environment " +
		"(an earlier version of the rule wrote it; it is not synced any more)"
}

// ownBranch is the rule's own branch, unless branch_map sends it to an environment (then "").
func ownBranch(rule Rule) string {
	branch := rule.Branch
	if branch == "" {
		branch = "main"
	}
	if _, mapped := rule.BranchMap[branch]; mapped {
		return ""
	}
	return branch
}

// maySync is what a direct push to the branch feeding env needs, in one of the
// repositories the rule names. It returns the branch it judged and, when the
// answer is no, why.
func (e *Engine) maySync(ctx context.Context, rule Rule, c *caller, env string) (ok bool, branch, why, deniedBy string, err error) {
	branch = branchFor(rule, env)
	// Nothing to sync is no for everyone, admins included: "allowed" must mean the sync can happen.
	if branch == "" {
		return false, "", nothingFeeds(rule, env), DeniedByRule, nil
	}
	if c.admin {
		return true, branch, "", "", nil
	}
	if c.token == "" {
		return false, branch, "not signed in to " + rule.Platform, DeniedByGit, nil
	}
	p, err := e.rules.GetPlatformForRule(ctx, rule.Platform, rule.Datasource)
	if err != nil {
		return false, branch, "", "", err
	}
	var named []string
	for _, repo := range rule.Include {
		if isGlob(repo) || matchesAny(repo, rule.Exclude) {
			continue
		}
		named = append(named, repo)
		pushes, err := e.canPush(ctx, p, rule.Platform, c.token, repo, branch)
		if err != nil {
			return false, branch, "", "", err
		}
		if pushes {
			return true, branch, "", "", nil
		}
	}
	if len(named) == 0 {
		return false, branch, "the " + rule.Datasource + " rule names no repository to push to (only patterns)", DeniedByRule, nil
	}
	return false, branch, c.login + " on " + c.platform + " may not push to " + branch +
		" of " + strings.Join(named, ", ") + " (the branch that feeds " + env + ")", DeniedByGit, nil
}

// ── decisions ────────────────────────────────────────────────────────────────

const deniedPrefix = "permission denied: "

// Decide answers one question. A "no" is a Decision with Allowed false; an error is
// a question that could not be answered: ErrNoRule, *AmbiguousRuleError,
// *PlatformRequiredError, models.ErrTokenRejected, *PolicyError, or the platform
// being unreachable.
func (e *Engine) Decide(ctx context.Context, req Request) (*Decision, error) {
	if req.Rule == nil && req.RuleDatasource == "" {
		return e.decideCaller(ctx, req)
	}
	rule, err := e.ruleOf(req)
	if err != nil {
		return nil, err
	}
	c, err := e.callerFor(ctx, req.Identities, rule)
	if err != nil {
		return nil, err
	}
	return e.judge(ctx, req, rule, c)
}

func (e *Engine) ruleOf(req Request) (Rule, error) {
	if req.Rule != nil {
		return *req.Rule, nil
	}
	return e.ResolveRule(req.RulePlatform, req.RuleDatasource)
}

// decideCaller is a decision about the caller alone: a good token on one platform.
func (e *Engine) decideCaller(ctx context.Context, req Request) (*Decision, error) {
	if len(req.Identities) == 0 {
		return nil, models.ErrTokenRejected
	}
	id := req.Identities[0]
	if id.Platform == "" {
		id.Platform = req.RulePlatform
	}
	who, err := e.Identify(ctx, id)
	if err != nil {
		return nil, err
	}
	d := &Decision{Allowed: true, Who: who, Permission: "none"}
	if who.Admin {
		d.Permission = "admin"
	}
	if err := e.applyPolicy(ctx, req, Rule{Platform: who.Platform}, d, ""); err != nil {
		return nil, err
	}
	return d, nil
}

// judge decides req against rule for an identified caller.
func (e *Engine) judge(ctx context.Context, req Request, rule Rule, c *caller) (*Decision, error) {
	perm, err := e.rulePermission(ctx, rule, c)
	if err != nil {
		return nil, err
	}
	d := &Decision{
		Allowed:    true,
		Who:        Who{Platform: rule.Platform, Login: c.login, Admin: c.admin},
		Permission: permissionName(perm, c.admin),
	}
	deny := func(by, format string, args ...any) *Decision {
		d.Allowed, d.DeniedBy, d.Reason = false, by, deniedPrefix+fmt.Sprintf(format, args...)
		return d
	}
	if req.tenantChecked {
		d.Tenant = req.TenantID
	} else {
		tenant, why, err := e.tenantFor(ctx, req.TenantID, rule, c)
		if err != nil {
			return nil, err
		}
		if why != "" {
			return deny(DeniedByTenant, "%s", why), nil
		}
		d.Tenant = tenant
	}
	needRead := func() *Decision {
		return deny(DeniedByGit, "%s on %s needs read access to %s (the repositories of the %s rule)",
			c.login, rule.Platform, strings.Join(rule.Include, ", "), rule.Datasource)
	}

	switch req.Action {
	case Sync:
		ok, branch, why, by, err := e.maySync(ctx, rule, c, req.Env)
		if err != nil {
			return nil, err
		}
		d.Branch = branch
		if !ok {
			return deny(by, "%s", why), nil
		}
	case Read, DryRun, Validate, List:
		if req.Action == DryRun {
			d.Branch = branchFor(rule, req.Env) // what a re-sync reads, though nothing is written
		}
		if !perm.Read {
			return needRead(), nil
		}
		if req.Action == DryRun && d.Branch == "" && req.Env != "" {
			// Allowed must mean possible: a dry run reads the branch that feeds the environment.
			return deny(DeniedByRule, "%s", nothingFeeds(rule, req.Env)), nil
		}
		if req.Action == Read && req.SourceRepo != "" && !c.admin {
			seen, err := e.mayReadRepo(ctx, rule, c, req.SourceRepo)
			if err != nil {
				return nil, err
			}
			if !seen {
				return deny(DeniedByGit, "%s on %s may not read %s", c.login, rule.Platform, req.SourceRepo), nil
			}
		}
	default:
		return nil, fmt.Errorf("authz: unknown action %q", req.Action)
	}

	if err := e.applyPolicy(ctx, req, rule, d, req.Env); err != nil {
		return nil, err
	}
	return d, nil
}

// mayReadRepo is whether the caller may read repo, a repository an environment was
// synced from. That decides whether the environment is visible, never what the
// caller may do to the rule.
func (e *Engine) mayReadRepo(ctx context.Context, rule Rule, c *caller, repo string) (bool, error) {
	if c.token == "" {
		return false, nil
	}
	p, err := e.rules.GetPlatformForRule(ctx, rule.Platform, rule.Datasource)
	if err != nil {
		return false, err
	}
	perm, err := e.permission(ctx, p, rule.Platform, c.token, repo)
	return perm.Read, err
}

// policyInput is the document the policy is asked about, and whether the action is
// one it covers: sync, dry runs and validate. Reading is Git's alone.
func policyInput(req Request, rule Rule, d *Decision) (auth.OPAInput, bool) {
	input := auth.OPAInput{
		User: auth.OPAUser{
			Login:        d.Login,
			Platform:     d.Platform,
			Permission:   d.Permission,
			Admin:        d.Admin,
			Repositories: rule.Include,
		},
		Resource:   req.Resource,
		Datasource: req.RuleDatasource,
		Env:        strings.ToLower(req.Env),
		FilePath:   req.FilePath,
		TenantID:   coalesceTenant(d.Tenant, req.TenantID),
		Via:        req.Via,
		Branch:     d.Branch,
	}
	if input.Resource == "" {
		input.Resource = "datasource"
	}
	if rule.Datasource != "" {
		input.Datasource = rule.Datasource
	}
	switch req.Action {
	case Sync:
		input.Action = "sync"
	case DryRun:
		input.Action, input.DryRun = "sync", true
	case Validate:
		input.Action = "validate"
	default:
		return input, false
	}
	return input, true
}

// applyPolicy runs the policy on a decision Git allowed, and turns it into a denial
// when the policy says no.
func (e *Engine) applyPolicy(ctx context.Context, req Request, rule Rule, d *Decision, env string) error {
	if e.opa == nil || !d.Allowed {
		return nil
	}
	input, covered := policyInput(req, rule, d)
	if !covered {
		return nil
	}
	ok, err := e.opa.Allow(ctx, input)
	if err != nil {
		return &PolicyError{Err: err}
	}
	if ok {
		return nil
	}
	d.Allowed, d.DeniedBy = false, DeniedByPolicy
	switch {
	case req.Action == Sync || req.Action == DryRun:
		d.Reason = "policy denied sync to env=" + env + " datasource=" + input.Datasource + " — try --dry-run"
		if req.Action == DryRun {
			d.Reason = "policy denied the dry run to env=" + env + " datasource=" + input.Datasource
		}
	case input.Resource == "config":
		d.Reason = "policy denied validate-config for " + req.FilePath
	default:
		d.Reason = "policy denied validate for datasource=" + input.Datasource
	}
	return nil
}

func permissionName(p models.RepoPermission, admin bool) string {
	switch {
	case admin || p.Admin:
		return "admin"
	case p.Write:
		return "write"
	case p.Read:
		return "read"
	}
	return "none"
}

// ── many rules at once ───────────────────────────────────────────────────────

// ReadableRules judges rules for one caller and keeps the ones they may read. It
// asks for what the platforms know once per repository, however many rules cover
// it, and in parallel. A token the platform rejects fails the call; a platform that
// cannot be reached makes only its rules unreadable, because unknown is not allowed.
func (e *Engine) ReadableRules(ctx context.Context, ids []Identity, rules []Rule, tenant string) (*Readable, error) {
	if len(ids) == 0 {
		return nil, models.ErrTokenRejected
	}
	type result struct {
		access   RuleAccess
		readable bool
		admin    bool
		err      error
	}
	results := make([]result, len(rules))
	sem := make(chan struct{}, 8)
	var wg sync.WaitGroup
	for i := range rules {
		wg.Add(1)
		sem <- struct{}{}
		go func(i int) {
			defer wg.Done()
			defer func() { <-sem }()
			rule := rules[i]
			c, err := e.callerFor(ctx, ids, rule)
			if err != nil {
				results[i].err = err
				return
			}
			perm, err := e.rulePermission(ctx, rule, c)
			if err != nil {
				results[i].err = err
				return
			}
			results[i].admin = c.admin
			if !perm.Read {
				return
			}
			reach, err := e.tenantReach(ctx, tenant, rule, c)
			if err != nil {
				results[i].err = err
				return
			}
			if !reach {
				return
			}
			access := RuleAccess{Index: i, Permission: permissionName(perm, c.admin)}
			if perm.Write {
				access.CanSync = e.canSyncAny(ctx, rule, c, tenant)
			}
			results[i].access, results[i].readable = access, true
		}(i)
	}
	wg.Wait()

	out := &Readable{}
	for i, r := range results {
		if r.admin {
			out.Admin = true
		}
		if r.err != nil {
			if errors.Is(r.err, models.ErrTokenRejected) {
				return nil, r.err
			}
			slog.Warn("authz: a rule could not be judged and is treated as unreadable",
				"platform", rules[i].Platform, "datasource", rules[i].Datasource, "error", r.err)
			continue
		}
		if r.readable {
			out.Rules = append(out.Rules, r.access)
		}
	}
	return out, nil
}

// canSyncAny is whether the caller may sync at least one of the rule's environments:
// the ones the caller listed, else the ones the rule itself produces: its branch_map's, its
// file_path_map's, and "default" when branch names are not environments.
func (e *Engine) canSyncAny(ctx context.Context, rule Rule, c *caller, tenant string) bool {
	envs := rule.Envs
	if len(envs) == 0 {
		seen := map[string]bool{}
		add := func(env string) {
			if !seen[env] {
				seen[env] = true
				envs = append(envs, env)
			}
		}
		for _, env := range rule.BranchMap {
			add(env)
		}
		for _, env := range rule.FilePathEnvs {
			add(env)
		}
		if !rule.BranchAsEnv {
			add(defaultEnv)
		}
	}
	if len(envs) == 0 && c.admin {
		return true // an admin may sync whatever the rule feeds, even before it has fed anything
	}
	sort.Strings(envs)
	for _, env := range envs {
		d, err := e.judge(ctx, Request{Action: Sync, Env: env, Via: ViaUI, TenantID: tenant, RuleDatasource: rule.Datasource, tenantChecked: true}, rule, c)
		if err != nil {
			slog.Debug("authz: canSync", "datasource", rule.Datasource, "env", env, "error", err)
			continue
		}
		if d.Allowed {
			return true
		}
	}
	return false
}

// ── repository patterns ──────────────────────────────────────────────────────

func isGlob(p string) bool {
	return strings.ContainsAny(p, "*?[")
}

func matchesAny(repo string, patterns []string) bool {
	repo = strings.ToLower(repo)
	for _, p := range patterns {
		if ok, _ := path.Match(strings.ToLower(p), repo); ok {
			return true
		}
	}
	return false
}

// timeNow is the clock the grant check judges lifetimes by.
var timeNow = time.Now

// coalesceTenant is the checked tenant when there is one, else what the request named.
func coalesceTenant(checked, requested string) string {
	if checked != "" {
		return checked
	}
	return requested
}
