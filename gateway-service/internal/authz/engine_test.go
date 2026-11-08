package authz_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"gateway-service/internal/auth"
	"gateway-service/internal/authz"
	"gateway-service/internal/models"
	pb "vartrack/libs/gen/vartrack/v1/models"
)

// fakeGit is a Git platform: tokens are logins, perms[login][repo] what each may
// do there. It counts its questions.
type fakeGit struct {
	models.Platform
	perms map[string]map[string]models.RepoPermission
	calls atomic.Int64
	// failing makes every question an upstream error.
	failing atomic.Bool
}

func (f *fakeGit) CallerLogin(_ context.Context, token string) (string, error) {
	f.calls.Add(1)
	if f.failing.Load() {
		return "", errors.New("platform unreachable")
	}
	if _, ok := f.perms[token]; !ok {
		return "", models.ErrTokenRejected
	}
	return token, nil
}

func (f *fakeGit) CallerPermission(_ context.Context, token, repo string) (models.RepoPermission, error) {
	f.calls.Add(1)
	if f.failing.Load() {
		return models.RepoPermission{}, errors.New("platform unreachable")
	}
	return f.perms[token][repo], nil
}

func (f *fakeGit) CallerRepos(_ context.Context, token string) (map[string]models.RepoPermission, error) {
	f.calls.Add(1)
	return f.perms[token], nil
}

// CallerCanPush: "main" is protected — only admins push to it.
func (f *fakeGit) CallerCanPush(_ context.Context, token, repo, branch string) (bool, error) {
	f.calls.Add(1)
	p := f.perms[token][repo]
	if branch == "main" {
		return p.Admin, nil
	}
	return p.Write, nil
}

type fakeRules struct {
	rules     []*pb.Rule
	platforms map[string]*fakeGit
	names     []string
}

func (f fakeRules) FindRule(platform, ds string) *pb.Rule {
	for _, r := range f.rules {
		if r.GetPlatform() == platform && r.GetDatasource() == ds {
			return r
		}
	}
	return nil
}

func (f fakeRules) RulesForDatasource(ds string) []*pb.Rule {
	var rules []*pb.Rule
	for _, r := range f.rules {
		if r.GetDatasource() == ds {
			rules = append(rules, r)
		}
	}
	return rules
}

func (f fakeRules) Rules() []*pb.Rule { return f.rules }

func (f fakeRules) GetPlatformForRule(_ context.Context, platform, _ string) (models.Platform, error) {
	p, ok := f.platforms[platform]
	if !ok {
		return nil, errors.New("no such platform " + platform)
	}
	return p, nil
}

func (f fakeRules) PlatformNames() []string { return f.names }

var (
	read  = models.RepoPermission{Read: true}
	write = models.RepoPermission{Read: true, Write: true}
	owner = models.RepoPermission{Read: true, Write: true, Admin: true}
)

type world struct {
	rules  fakeRules
	gitea  *fakeGit
	team   *fakeGit
	engine *authz.Engine
}

var develop = "develop"

func newWorld(t *testing.T, policy string, opts ...authz.Option) *world {
	t.Helper()
	gitea := &fakeGit{perms: map[string]map[string]models.RepoPermission{
		"wes":  {"acme/configs": write},
		"rae":  {"acme/configs": read},
		"ada":  {"acme/configs": owner},
		"gus":  {"acme/legacy-app": write, "acme/web-app": read},
		"lee":  {"acme/legacy-app": write},
		"root": {},
	}}
	team := &fakeGit{perms: map[string]map[string]models.RepoPermission{
		"tia": {"payments/config": write},
	}}
	rules := fakeRules{
		rules: []*pb.Rule{
			{Platform: "gitea", Datasource: "redis", Repositories: []string{"acme/configs"},
				BranchMap: map[string]string{"main": "production", "develop": "staging"}},
			{Platform: "gitea", Datasource: "s3", Repositories: []string{"acme/*-app"},
				ExcludeRepositories: []string{"acme/legacy-*"}, BranchAsEnv: true},
			{Platform: "gitea-team", Datasource: "redis-team", Repositories: []string{"payments/config"}},
			// One datasource, written by a rule on each of two platforms.
			{Platform: "gitea", Datasource: "mongo-primary", Repositories: []string{"acme/configs"},
				BranchMap: map[string]string{"develop": "staging"}},
			{Platform: "gitea-team", Datasource: "mongo-primary", Repositories: []string{"payments/config"},
				BranchMap: map[string]string{"develop": "staging"}},
			// No environment strategy: every push lands in "default"; the rule's own branch feeds it.
			{Platform: "gitea", Datasource: "plain", Repositories: []string{"acme/configs"}},
			// Environments by file path; the rule reads its own branch.
			{Platform: "gitea", Datasource: "by-path", Repositories: []string{"acme/configs"}, Branch: &develop,
				FilePathMap: map[string]string{"team-a": "team-a/app.yaml", "team-b": "team-b/app.yaml"}},
		},
		platforms: map[string]*fakeGit{"gitea": gitea, "gitea-team": team},
		names:     []string{"gitea", "gitea-team"},
	}
	if policy != "" {
		opa, err := auth.NewOPAEvaluatorEmbedded(policy)
		if err != nil {
			t.Fatal(err)
		}
		opts = append(opts, authz.WithPolicy(opa))
	}
	return &world{rules, gitea, team, authz.New(rules, []string{"Gitea:Root"}, opts...)}
}

func (w *world) ask(t *testing.T, token string, action authz.Action, platform, datasource, env string) *authz.Decision {
	t.Helper()
	d, err := w.engine.Decide(context.Background(), authz.Request{
		Identities: []authz.Identity{{Token: token}},
		Action:     action, RulePlatform: platform, RuleDatasource: datasource, Env: env,
	})
	if err != nil {
		t.Fatalf("%s %s %s/%s: %v", token, action, datasource, env, err)
	}
	return d
}

// ── the decision table ───────────────────────────────────────────────────────

var policies = map[string]string{
	"freeze-staging": `package vartrack.authz
default allow = false
allow { input.action != "sync" }
allow { input.action == "sync"; input.env != "staging" }`,
	"deny-all": `package vartrack.authz
default allow = false`,
	"allow-all": `package vartrack.authz
default allow = true`,
	"no-sync-from-ui": `package vartrack.authz
default allow = false
allow { input.via != "ui" }`,
}

type row struct {
	Name       string `json:"name"`
	Token      string `json:"token"`
	Action     string `json:"action"`
	Datasource string `json:"datasource"`
	Env        string `json:"env"`
	SourceRepo string `json:"source_repo"`
	Policy     string `json:"policy"`
	Via        string `json:"via"`

	Allowed    bool   `json:"allowed"`
	DeniedBy   string `json:"denied_by"`
	Permission string `json:"permission"`
	Branch     string `json:"branch"`
	Reason     string `json:"reason"`
}

func TestDecisionTable(t *testing.T) {
	raw, err := os.ReadFile("testdata/decisions.json")
	if err != nil {
		t.Fatal(err)
	}
	var rows []row
	if err := json.Unmarshal(raw, &rows); err != nil {
		t.Fatal(err)
	}
	for _, r := range rows {
		t.Run(r.Name, func(t *testing.T) {
			w := newWorld(t, policies[r.Policy])
			d, err := w.engine.Decide(context.Background(), authz.Request{
				Identities: []authz.Identity{{Token: r.Token}},
				Action:     authz.Action(r.Action), RuleDatasource: r.Datasource, Env: r.Env,
				SourceRepo: r.SourceRepo, Via: r.Via,
			})
			if err != nil {
				t.Fatal(err)
			}
			if d.Allowed != r.Allowed || d.DeniedBy != r.DeniedBy {
				t.Errorf("allowed=%v denied_by=%q (%s), want allowed=%v denied_by=%q",
					d.Allowed, d.DeniedBy, d.Reason, r.Allowed, r.DeniedBy)
			}
			if d.Permission != r.Permission {
				t.Errorf("permission = %q, want %q", d.Permission, r.Permission)
			}
			if r.Branch != "" && d.Branch != r.Branch {
				t.Errorf("branch = %q, want %q", d.Branch, r.Branch)
			}
			if r.Reason != "" && d.Reason != r.Reason {
				t.Errorf("reason = %q, want %q", d.Reason, r.Reason)
			}
			if r.Allowed && d.Reason != "" {
				t.Errorf("an allowed decision carries a reason: %q", d.Reason)
			}
		})
	}
}

// ── errors the caller maps to a status ───────────────────────────────────────

func TestErrors(t *testing.T) {
	w := newWorld(t, "")
	ctx := context.Background()
	ask := func(token, platform, datasource string) error {
		_, err := w.engine.Decide(ctx, authz.Request{
			Identities: []authz.Identity{{Token: token}}, Action: authz.Read,
			RulePlatform: platform, RuleDatasource: datasource,
		})
		return err
	}
	if err := ask("mallory", "", "redis"); !errors.Is(err, models.ErrTokenRejected) {
		t.Errorf("unknown token: %v", err)
	}
	if err := ask("wes", "", "nowhere"); !errors.Is(err, authz.ErrNoRule) {
		t.Errorf("unknown datasource: %v", err)
	}
	if err := ask("wes", "github", "mongo-primary"); !errors.Is(err, authz.ErrNoRule) {
		t.Errorf("a platform with no rule for the datasource: %v", err)
	}
	var ambiguous *authz.AmbiguousRuleError
	err := ask("wes", "", "mongo-primary")
	if !errors.As(err, &ambiguous) {
		t.Fatalf("rules on two platforms, none named: %v", err)
	}
	if got := ambiguous.Platforms; len(got) != 2 || got[0] != "gitea" || got[1] != "gitea-team" {
		t.Errorf("platforms offered = %v", got)
	}
	if ambiguous.Error() != "datasource mongo-primary has rules on gitea, gitea-team; say which with --platform" {
		t.Errorf("message = %q", ambiguous.Error())
	}
	var unknown *authz.UnknownPlatformError
	if _, err := w.engine.Identify(ctx, authz.Identity{Platform: "nowhere", Token: "wes"}); !errors.As(err, &unknown) {
		t.Errorf("a platform the bundle does not have: %v", err)
	}
	if _, err := w.engine.Decide(ctx, authz.Request{Action: authz.Read, RuleDatasource: "redis"}); !errors.Is(err, models.ErrTokenRejected) {
		t.Errorf("no token at all: %v", err)
	}
}

func TestARuleIsIdentifiedByItsPlatformAndDatasource(t *testing.T) {
	w := newWorld(t, "")
	if d := w.ask(t, "wes", authz.Sync, "gitea", "mongo-primary", "staging"); !d.Allowed || d.Platform != "gitea" {
		t.Errorf("wes on gitea: %+v", d)
	}
	if d := w.ask(t, "tia", authz.Sync, "gitea-team", "mongo-primary", "staging"); !d.Allowed || d.Platform != "gitea-team" {
		t.Errorf("tia on gitea-team: %+v", d)
	}
	// A Gitea-team token on the Gitea rule is the Gitea platform's to refuse.
	_, err := w.engine.Decide(context.Background(), authz.Request{
		Identities: []authz.Identity{{Token: "tia"}}, Action: authz.Read, RulePlatform: "gitea", RuleDatasource: "mongo-primary",
	})
	if !errors.Is(err, models.ErrTokenRejected) {
		t.Errorf("a Gitea-team token on the Gitea rule: %v", err)
	}
	// A datasource with one rule needs no platform.
	if d := w.ask(t, "tia", authz.Read, "", "redis-team", ""); d.Platform != "gitea-team" {
		t.Errorf("one rule, no platform named: %+v", d)
	}
}

// ── a token is shown to one platform ─────────────────────────────────────────

func TestATokenIsNeverShownToEveryPlatform(t *testing.T) {
	w := newWorld(t, "")
	ctx := context.Background()

	// A question about the caller alone, with two platforms and none named: refused,
	// and no platform is asked.
	_, err := w.engine.Decide(ctx, authz.Request{Identities: []authz.Identity{{Token: "tia"}}, Action: authz.Validate})
	var need *authz.PlatformRequiredError
	if !errors.As(err, &need) {
		t.Fatalf("err = %v, want a *PlatformRequiredError", err)
	}
	if w.gitea.calls.Load()+w.team.calls.Load() != 0 {
		t.Error("a platform was asked although none was named")
	}
	if !strings.Contains(need.Error(), "gitea, gitea-team") {
		t.Errorf("message = %q", need.Error())
	}

	// Named, only that platform is asked.
	d, err := w.engine.Decide(ctx, authz.Request{Identities: []authz.Identity{{Platform: "gitea-team", Token: "tia"}}, Action: authz.Validate})
	if err != nil || d.Name() != "tia@gitea-team" {
		t.Fatalf("named: %+v, %v", d, err)
	}
	if w.gitea.calls.Load() != 0 {
		t.Error("the token was shown to a platform it was not issued by")
	}

	// A bundle with one platform needs none named.
	one := newWorld(t, "")
	one.rules.names = []string{"gitea"}
	who, err := authz.New(one.rules, nil).Identify(ctx, authz.Identity{Token: "wes"})
	if err != nil || who.Platform != "gitea" || who.Login != "wes" {
		t.Errorf("one platform: %+v, %v", who, err)
	}
}

// ── rules the caller supplies (the web UI's tenants) ─────────────────────────

func TestARuleSuppliedAsFactsIsJudgedLikeABundleRule(t *testing.T) {
	w := newWorld(t, "")
	ctx := context.Background()
	rule := authz.Rule{
		Platform: "gitea", Datasource: "tenant-redis", Include: []string{"acme/configs"},
		BranchMap: map[string]string{"main": "production", "develop": "staging"},
	}
	ask := func(token string, action authz.Action, env string) *authz.Decision {
		d, err := w.engine.Decide(ctx, authz.Request{
			Identities: []authz.Identity{{Platform: "gitea", Token: token}},
			Rule:       &rule, Action: action, Env: env, Via: authz.ViaUI,
		})
		if err != nil {
			t.Fatal(err)
		}
		return d
	}
	if d := ask("wes", authz.Sync, "staging"); !d.Allowed || d.Branch != "develop" {
		t.Errorf("writer, unprotected: %+v", d)
	}
	if d := ask("wes", authz.Sync, "production"); d.Allowed {
		t.Errorf("writer, protected branch: %+v", d)
	}
	if d := ask("rae", authz.DryRun, "production"); !d.Allowed {
		t.Errorf("reader dry run: %+v", d)
	}
}

func TestAnAdminOnAnyPlatformTheCallerSignedInToIsAnAdmin(t *testing.T) {
	w := newWorld(t, "")
	rule := authz.Rule{Platform: "gitea-team", Datasource: "x", Include: []string{"payments/config"},
		BranchAsEnv: true}
	d, err := w.engine.Decide(context.Background(), authz.Request{
		Identities: []authz.Identity{{Platform: "gitea", Token: "root"}}, // no identity on gitea-team
		Rule:       &rule, Action: authz.Sync, Env: "production",
	})
	if err != nil || !d.Allowed || !d.Admin {
		t.Errorf("admin without an identity on the rule's platform: %+v, %v", d, err)
	}
	d, err = w.engine.Decide(context.Background(), authz.Request{
		Identities: []authz.Identity{{Platform: "gitea", Token: "wes"}},
		Rule:       &rule, Action: authz.Read,
	})
	if err != nil || d.Allowed || d.Permission != "none" {
		t.Errorf("no identity on the rule's platform and no admin: %+v, %v", d, err)
	}
}

// ── many rules at once ───────────────────────────────────────────────────────

func TestReadableRulesKeepsOnlyWhatTheCallerMayRead(t *testing.T) {
	w := newWorld(t, "")
	rules := w.engine.BundleRules()
	got, err := w.engine.ReadableRules(context.Background(), []authz.Identity{{Platform: "gitea", Token: "rae"}}, rules, "default")
	if err != nil {
		t.Fatal(err)
	}
	byIndex := map[int]authz.RuleAccess{}
	for _, r := range got.Rules {
		byIndex[r.Index] = r
	}
	// redis (0), mongo-primary on gitea (3), plain (5) and by-path (6): rae reads acme/configs.
	// s3 (1): rae has nothing in acme/*-app. The gitea-team rules have no identity.
	if len(byIndex) != 4 || byIndex[0].Permission != "read" || byIndex[3].Permission != "read" ||
		byIndex[5].Permission != "read" || byIndex[6].Permission != "read" {
		t.Errorf("readable = %+v", got.Rules)
	}
	if byIndex[0].CanSync {
		t.Error("a reader can sync")
	}

	got, err = w.engine.ReadableRules(context.Background(), []authz.Identity{{Platform: "gitea", Token: "wes"}}, rules, "default")
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range got.Rules {
		if r.Index == 0 && !r.CanSync { // develop is not protected
			t.Error("a writer cannot sync staging")
		}
	}

	got, err = w.engine.ReadableRules(context.Background(), []authz.Identity{{Platform: "gitea", Token: "root"}}, rules, "default")
	if err != nil || !got.Admin || len(got.Rules) != len(rules) {
		t.Fatalf("admin: %+v, %v", got, err)
	}
	for _, r := range got.Rules {
		if !r.CanSync { // including the rules that feed no environment yet
			t.Errorf("an admin cannot sync %s", rules[r.Index].Datasource)
		}
	}

	if _, err = w.engine.ReadableRules(context.Background(), []authz.Identity{{Platform: "gitea", Token: "mallory"}}, rules, "default"); !errors.Is(err, models.ErrTokenRejected) {
		t.Errorf("a rejected token: %v", err)
	}
}

func TestAPlatformThatCannotBeAskedMakesItsRulesUnreadable(t *testing.T) {
	w := newWorld(t, "", authz.WithCacheTTL(0))
	w.team.failing.Store(true)
	got, err := w.engine.ReadableRules(context.Background(), []authz.Identity{
		{Platform: "gitea", Token: "wes"}, {Platform: "gitea-team", Token: "tia"},
	}, w.engine.BundleRules(), "default")
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range got.Rules {
		if w.engine.BundleRules()[r.Index].Platform == "gitea-team" {
			t.Errorf("a rule on an unreachable platform is readable: %+v", r)
		}
	}
	if len(got.Rules) == 0 {
		t.Error("the reachable platform's rules were dropped too")
	}
}

// ── the cache ────────────────────────────────────────────────────────────────

func TestPlatformAnswersAreCached(t *testing.T) {
	w := newWorld(t, "")
	for i := 0; i < 5; i++ {
		w.ask(t, "wes", authz.Sync, "", "redis", "staging")
	}
	first := w.gitea.calls.Load()
	if first == 0 || first > 3 { // login, permission, can-push
		t.Fatalf("the platform was asked %d times for one question", first)
	}
	if w.ask(t, "wes", authz.Sync, "", "redis", "staging"); w.gitea.calls.Load() != first {
		t.Error("a repeated question was asked again")
	}
}

func TestConcurrentAskersShareOneRoundTrip(t *testing.T) {
	w := newWorld(t, "")
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w.ask(t, "rae", authz.Read, "", "redis", "")
		}()
	}
	wg.Wait()
	if n := w.gitea.calls.Load(); n > 2 { // login and permission, once each
		t.Errorf("the platform was asked %d times by 20 concurrent askers", n)
	}
}

func TestAnErrorIsNeverCached(t *testing.T) {
	w := newWorld(t, "")
	w.gitea.failing.Store(true)
	if _, err := w.engine.Decide(context.Background(), authz.Request{
		Identities: []authz.Identity{{Token: "wes"}}, Action: authz.Read, RuleDatasource: "redis",
	}); err == nil {
		t.Fatal("an unreachable platform answered")
	}
	w.gitea.failing.Store(false)
	if d := w.ask(t, "wes", authz.Read, "", "redis", ""); !d.Allowed {
		t.Errorf("the failure outlived the outage: %+v", d)
	}
}

func TestARejectedTokenIsRememberedBriefly(t *testing.T) {
	w := newWorld(t, "")
	ask := func() error {
		_, err := w.engine.Decide(context.Background(), authz.Request{
			Identities: []authz.Identity{{Token: "mallory"}}, Action: authz.Read, RuleDatasource: "redis",
		})
		return err
	}
	for i := 0; i < 4; i++ {
		if err := ask(); !errors.Is(err, models.ErrTokenRejected) {
			t.Fatalf("err = %v", err)
		}
	}
	if n := w.gitea.calls.Load(); n != 1 {
		t.Errorf("a rejected token was shown to the platform %d times", n)
	}
}

func TestForgetDropsATokensAnswers(t *testing.T) {
	w := newWorld(t, "")
	w.ask(t, "wes", authz.Read, "", "redis", "")
	before := w.gitea.calls.Load()
	w.engine.Forget([]authz.Identity{{Platform: "gitea", Token: "wes"}})
	w.ask(t, "wes", authz.Read, "", "redis", "")
	if w.gitea.calls.Load() == before {
		t.Error("answers survived Forget")
	}
	// Another token's answers are untouched.
	w.ask(t, "rae", authz.Read, "", "redis", "")
	mark := w.gitea.calls.Load()
	w.engine.Forget([]authz.Identity{{Platform: "gitea", Token: "wes"}})
	w.ask(t, "rae", authz.Read, "", "redis", "")
	if w.gitea.calls.Load() != mark {
		t.Error("Forget dropped another token's answers")
	}
}

func TestACacheOfZeroTTLAsksEveryTime(t *testing.T) {
	w := newWorld(t, "", authz.WithCacheTTL(0))
	w.ask(t, "wes", authz.Read, "", "redis", "")
	first := w.gitea.calls.Load()
	w.ask(t, "wes", authz.Read, "", "redis", "")
	if w.gitea.calls.Load() != 2*first {
		t.Errorf("calls = %d then %d", first, w.gitea.calls.Load())
	}
}

// ── the policy ───────────────────────────────────────────────────────────────

func TestAPolicyErrorRefusesTheRequest(t *testing.T) {
	// A policy server that cannot be reached.
	opa := auth.NewOPAEvaluatorServer("http://127.0.0.1:1/v1/data/vartrack/authz/allow")
	w := newWorld(t, "")
	e := authz.New(w.rules, nil, authz.WithPolicy(opa))
	_, err := e.Decide(context.Background(), authz.Request{
		Identities: []authz.Identity{{Token: "wes"}}, Action: authz.Sync, RuleDatasource: "redis", Env: "staging",
	})
	var perr *authz.PolicyError
	if !errors.As(err, &perr) {
		t.Errorf("err = %v, want a *PolicyError", err)
	}
}

func TestACallerOnlyDecisionRunsThePolicy(t *testing.T) {
	w := newWorld(t, `package vartrack.authz
default allow = false
allow { input.action == "validate"; input.resource == "config"; input.file_path == "vartrack.json" }`)
	ctx := context.Background()
	d, err := w.engine.Decide(ctx, authz.Request{
		Identities: []authz.Identity{{Platform: "gitea", Token: "wes"}}, Action: authz.Validate,
		Resource: "config", FilePath: "vartrack.json",
	})
	if err != nil || !d.Allowed {
		t.Errorf("allowed by the policy: %+v, %v", d, err)
	}
	d, _ = w.engine.Decide(ctx, authz.Request{
		Identities: []authz.Identity{{Platform: "gitea", Token: "wes"}}, Action: authz.Validate,
		Resource: "config", FilePath: "other.json",
	})
	if d.Allowed || d.DeniedBy != authz.DeniedByPolicy || d.Reason != "policy denied validate-config for other.json" {
		t.Errorf("refused by the policy: %+v", d)
	}
}

func TestAdminsNameTheirPlatform(t *testing.T) {
	got, err := authz.NormalizeAdmins([]string{" Gitea:Root ", "github:octo", "", "alice"}, []string{"Gitea"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got, ",") != "gitea:root,github:octo,gitea:alice" {
		t.Errorf("admins = %v", got)
	}
	// With several platforms a bare login is ambiguous: "alice" on GitHub need not be "alice" on Gitea.
	if _, err := authz.NormalizeAdmins([]string{"alice"}, []string{"gitea", "github"}); err == nil ||
		!strings.Contains(err.Error(), "gitea:alice") {
		t.Errorf("bare admin with two platforms: %v", err)
	}
	// An admin of one platform is not an admin of another.
	w := newWorld(t, "")
	d := w.ask(t, "root", authz.Read, "", "redis", "")
	if !d.Admin {
		t.Error("gitea:root is not an admin on gitea")
	}
	if _, err := w.engine.Decide(context.Background(), authz.Request{
		Identities: []authz.Identity{{Platform: "gitea-team", Token: "tia"}}, Action: authz.Read, RulePlatform: "gitea-team", RuleDatasource: "redis-team",
	}); err != nil {
		t.Fatal(err)
	}
}
