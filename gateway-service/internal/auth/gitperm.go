package auth

// gitperm.go — the CLI API's authorization: Git permissions.
//
// varTrack keeps no roles of its own here. The caller's own Git token is
// asked of the rule's Git platform: who they are, and what they may do in
// the repositories the rule covers. The web UI decides the same way
// (orchestrator-service/app/ui/access.py):
//
//	a rule's repositories  its repository names and patterns, plus enabled
//	                       overrides, minus exclusions (config.cue);
//	read                   the caller can read one of them: validate, dry
//	                       runs, tasks;
//	write                  the caller can push to one of them: sync.
//
// "platform:login" entries of config.cue's ui.auth.admins may do anything.

import (
	"context"
	"errors"
	"path"
	"strings"

	pb "vartrack/libs/gen/vartrack/v1/models"
	"gateway-service/internal/models"
)

// ErrNoRule: no rule in config.cue syncs to the datasource asked about.
var ErrNoRule = errors.New("no rule syncs to this datasource")

// AmbiguousRuleError: rules on several platforms write to the datasource and
// the request does not name one.
type AmbiguousRuleError struct {
	Datasource string
	Platforms  []string
}

func (e *AmbiguousRuleError) Error() string {
	return "datasource " + e.Datasource + " has rules on " + strings.Join(e.Platforms, ", ") +
		"; say which with --platform"
}

// RuleSource is what the authorizer needs of the bundle.
type RuleSource interface {
	FindRule(platformName, datasourceName string) *pb.Rule
	RulesForDatasource(datasource string) []*pb.Rule
	GetPlatformForRule(ctx context.Context, platformName, datasourceName string) (models.Platform, error)
	PlatformNames() []string
	GetPlatform(ctx context.Context, name, managerName string) (models.Platform, error)
}

// GitCaller is who called, on one platform, and what they may do there.
type GitCaller struct {
	Platform   string
	Login      string
	Permission models.RepoPermission
	Admin      bool // in config.cue's ui.auth.admins
	Repos      []string
}

// Name is "login@platform", recorded with the caller's tasks.
func (c *GitCaller) Name() string {
	return c.Login + "@" + c.Platform
}

// PermissionName is "admin", "write", "read" or "none".
func (c *GitCaller) PermissionName() string {
	switch p := c.Permission; {
	case c.Admin || p.Admin:
		return "admin"
	case p.Write:
		return "write"
	case p.Read:
		return "read"
	}
	return "none"
}

// GitAuthorizer answers the CLI API's "who is this, and may they?".
type GitAuthorizer struct {
	rules  RuleSource
	admins map[string]bool // "platform:login", lower case
}

// NewGitAuthorizer authorizes against rules, with admins ("platform:login").
func NewGitAuthorizer(rules RuleSource, admins []string) *GitAuthorizer {
	set := map[string]bool{}
	for _, a := range admins {
		if a = strings.ToLower(strings.TrimSpace(a)); a != "" {
			set[a] = true
		}
	}
	return &GitAuthorizer{rules: rules, admins: set}
}

// ruleFor is the rule for platform and datasource. A rule is identified by
// the pair; platform may be left out when only one rule writes to the
// datasource, and is an *AmbiguousRuleError when several do.
func (a *GitAuthorizer) ruleFor(platform, datasource string) (*pb.Rule, error) {
	if platform != "" {
		if rule := a.rules.FindRule(platform, datasource); rule != nil {
			return rule, nil
		}
		return nil, ErrNoRule
	}
	rules := a.rules.RulesForDatasource(datasource)
	switch len(rules) {
	case 0:
		return nil, ErrNoRule
	case 1:
		return rules[0], nil
	}
	names := make([]string, len(rules))
	for i, r := range rules {
		names[i] = r.GetPlatform()
	}
	return nil, &AmbiguousRuleError{Datasource: datasource, Platforms: names}
}

// ForDatasource is the caller's permission on the rule for the platform named
// and the datasource (named "" when the datasource has one rule), asked of that
// rule's platform. It returns ErrNoRule when no rule fits, an
// *AmbiguousRuleError when the platform is needed, and models.ErrTokenRejected
// when the platform does not accept the caller's token.
func (a *GitAuthorizer) ForDatasource(ctx context.Context, token, named, datasource string) (*GitCaller, error) {
	rule, err := a.ruleFor(named, datasource)
	if err != nil {
		return nil, err
	}
	platformName := rule.GetPlatform()
	platform, err := a.rules.GetPlatformForRule(ctx, platformName, datasource)
	if err != nil {
		return nil, err
	}
	login, err := platform.CallerLogin(ctx, token)
	if err != nil {
		return nil, err
	}
	caller := &GitCaller{Platform: platformName, Login: login, Admin: a.admins[strings.ToLower(platformName+":"+login)]}
	include, exclude := rulePatterns(rule)
	caller.Repos = include
	if caller.Admin {
		caller.Permission = models.RepoPermission{Read: true, Write: true, Admin: true}
		return caller, nil
	}
	caller.Permission, err = a.rulePermission(ctx, platform, token, include, exclude)
	return caller, err
}

// MaySync reports whether the caller may write env with a real sync: what a
// direct push to the branch feeding env needs — on an unprotected branch push
// access, on a protected one what the platform allows — in one of the
// repositories the rule names. A sync bypasses Git review, so it may do no
// more than the caller could do in Git. The reason says why not. The rule is
// the caller's own: the one for the platform the caller was authenticated on.
func (a *GitAuthorizer) MaySync(ctx context.Context, token, datasource, env string, caller *GitCaller) (bool, string, error) {
	if caller.Admin {
		return true, "", nil
	}
	rule := a.rules.FindRule(caller.Platform, datasource)
	if rule == nil {
		return false, "", ErrNoRule
	}
	branch := branchFor(rule, env)
	if branch == "" {
		return false, "no branch in the " + datasource + " rule's branch_map feeds " + env, nil
	}
	platform, err := a.rules.GetPlatformForRule(ctx, rule.GetPlatform(), datasource)
	if err != nil {
		return false, "", err
	}
	include, exclude := rulePatterns(rule)
	var named []string
	for _, repo := range include {
		if isGlob(repo) || matchesAny(repo, exclude) {
			continue
		}
		named = append(named, repo)
		ok, err := platform.CallerCanPush(ctx, token, repo, branch)
		if err != nil {
			return false, "", err
		}
		if ok {
			return true, "", nil
		}
	}
	return false, caller.Login + " on " + caller.Platform + " may not push to " + branch +
		" of " + strings.Join(named, ", ") + " (the branch that feeds " + env + ")", nil
}

// branchFor is the branch whose pushes sync env: the rule's branch_map
// mapping to env, else env itself when branch names are environments.
func branchFor(rule *pb.Rule, env string) string {
	for branch, e := range rule.GetBranchMap() {
		if e == env {
			return branch
		}
	}
	if rule.GetBranchAsEnv() {
		return env
	}
	return ""
}

// Authenticate is who the caller is on the first platform of the bundle
// that accepts their token — for requests that name no datasource.
func (a *GitAuthorizer) Authenticate(ctx context.Context, token string) (*GitCaller, error) {
	var lastErr error = models.ErrTokenRejected
	for _, name := range a.rules.PlatformNames() {
		platform, err := a.rules.GetPlatform(ctx, name, "")
		if err != nil {
			lastErr = err
			continue
		}
		login, err := platform.CallerLogin(ctx, token)
		if err != nil {
			lastErr = err
			continue
		}
		return &GitCaller{Platform: name, Login: login, Admin: a.admins[strings.ToLower(name+":"+login)]}, nil
	}
	return nil, lastErr
}

func (a *GitAuthorizer) rulePermission(ctx context.Context, platform models.Platform, token string,
	include, exclude []string) (models.RepoPermission, error) {
	var perm models.RepoPermission
	hasGlob := false
	for _, repo := range include {
		if isGlob(repo) {
			hasGlob = true
			continue
		}
		if matchesAny(repo, exclude) {
			continue
		}
		p, err := platform.CallerPermission(ctx, token, repo)
		if err != nil {
			return perm, err
		}
		if perm = perm.Or(p); perm.Write {
			return perm, nil
		}
	}
	if !perm.Read && hasGlob {
		repos, err := platform.CallerRepos(ctx, token)
		if err != nil {
			return perm, err
		}
		for repo, p := range repos {
			if matchesAny(repo, include) && !matchesAny(repo, exclude) {
				perm = perm.Or(p)
			}
		}
	}
	return perm, nil
}

// ── repository patterns (as the gateway routes pushes) ───────────────────────

func rulePatterns(r *pb.Rule) (include, exclude []string) {
	include = append(include, r.GetRepositories()...)
	exclude = append(exclude, r.GetExcludeRepositories()...)
	for _, o := range r.GetOverrides() {
		if o.GetEnable() {
			include = append(include, o.GetMatchRepositories()...)
			exclude = append(exclude, o.GetExcludeRepositories()...)
		}
	}
	return include, exclude
}

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
