package auth_test

import (
	"context"
	"errors"
	"testing"

	"gateway-service/internal/auth"
	pb "vartrack/libs/gen/vartrack/v1/models"
	"gateway-service/internal/models"
)

// fakeGit is a Git platform: tokens are logins, perms[login][repo] what
// each may do there.
type fakeGit struct {
	models.Platform
	perms map[string]map[string]models.RepoPermission
}

func (f *fakeGit) CallerLogin(_ context.Context, token string) (string, error) {
	if _, ok := f.perms[token]; !ok {
		return "", models.ErrTokenRejected
	}
	return token, nil
}

func (f *fakeGit) CallerPermission(_ context.Context, token, repo string) (models.RepoPermission, error) {
	return f.perms[token][repo], nil
}

func (f *fakeGit) CallerRepos(_ context.Context, token string) (map[string]models.RepoPermission, error) {
	return f.perms[token], nil
}

// CallerCanPush: "main" is protected — only admins push to it.
func (f *fakeGit) CallerCanPush(_ context.Context, token, repo, branch string) (bool, error) {
	p := f.perms[token][repo]
	if branch == "main" {
		return p.Admin, nil
	}
	return p.Write, nil
}

type fakeRules struct {
	rules     []*pb.Rule
	platforms map[string]*fakeGit
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

func (f fakeRules) GetPlatformForRule(_ context.Context, platform, _ string) (models.Platform, error) {
	return f.platforms[platform], nil
}

func (f fakeRules) GetPlatform(_ context.Context, name, _ string) (models.Platform, error) {
	return f.platforms[name], nil
}

func (f fakeRules) PlatformNames() []string { return []string{"gitea", "gitea-team"} }

var (
	read  = models.RepoPermission{Read: true}
	write = models.RepoPermission{Read: true, Write: true}
)

func newAuthz() (*auth.GitAuthorizer, *fakeGit, *fakeGit) {
	gitea := &fakeGit{perms: map[string]map[string]models.RepoPermission{
		"wes":  {"acme/configs": write},
		"rae":  {"acme/configs": read},
		"gus":  {"acme/legacy-app": write, "acme/web-app": read},
		"root": {},
	}}
	team := &fakeGit{perms: map[string]map[string]models.RepoPermission{
		"tia": {"payments/config": write},
	}}
	rules := fakeRules{
		rules: []*pb.Rule{
			{Platform: "gitea", Datasource: "redis", Repositories: []string{"acme/configs"},
				BranchMap: map[string]string{"main": "production", "develop": "staging"}},
			{Platform: "gitea", Datasource: "s3", Repositories: []string{"acme/*-app"}, ExcludeRepositories: []string{"acme/legacy-*"}},
			{Platform: "gitea-team", Datasource: "redis-team", Repositories: []string{"payments/config"}},
			// One datasource, written by a rule on each of two platforms.
			{Platform: "gitea", Datasource: "mongo-primary", Repositories: []string{"acme/configs"},
				BranchMap: map[string]string{"develop": "staging"}},
			{Platform: "gitea-team", Datasource: "mongo-primary", Repositories: []string{"payments/config"},
				BranchMap: map[string]string{"develop": "staging"}},
		},
		platforms: map[string]*fakeGit{"gitea": gitea, "gitea-team": team},
	}
	return auth.NewGitAuthorizer(rules, []string{"Gitea:Root"}), gitea, team
}

func TestGitPermissionDecidesPerRule(t *testing.T) {
	a, _, _ := newAuthz()
	ctx := context.Background()
	for _, tc := range []struct {
		token, datasource, want string
	}{
		{"wes", "redis", "write"},
		{"rae", "redis", "read"},
		{"gus", "redis", "none"},
		{"gus", "s3", "read"}, // acme/legacy-app is excluded; acme/web-app matches the pattern
		{"root", "redis", "admin"},
	} {
		c, err := a.ForDatasource(ctx, tc.token, "", tc.datasource)
		if err != nil {
			t.Fatalf("%s on %s: %v", tc.token, tc.datasource, err)
		}
		if got := c.PermissionName(); got != tc.want {
			t.Errorf("%s on %s = %s, want %s", tc.token, tc.datasource, got, tc.want)
		}
	}
}

func TestGitAuthorizerErrors(t *testing.T) {
	a, _, _ := newAuthz()
	ctx := context.Background()
	if _, err := a.ForDatasource(ctx, "mallory", "", "redis"); !errors.Is(err, models.ErrTokenRejected) {
		t.Errorf("unknown token: err = %v", err)
	}
	if _, err := a.ForDatasource(ctx, "wes", "", "nowhere"); !errors.Is(err, auth.ErrNoRule) {
		t.Errorf("unknown datasource: err = %v", err)
	}
	c, err := a.Authenticate(ctx, "tia")
	if err != nil || c.Name() != "tia@gitea-team" {
		t.Errorf("Authenticate = %+v, %v", c, err)
	}
}

func TestASyncNeedsWhatAPushToTheEnvironmentsBranchNeeds(t *testing.T) {
	// The content of a sync may not come from Git: a sync of a local file
	// may do no more than a direct push could.
	a, _, _ := newAuthz()
	ctx := context.Background()
	for _, tc := range []struct {
		token, env string
		want       bool
	}{
		{"wes", "staging", true},     // develop is not protected: push access is enough
		{"wes", "production", false}, // main is protected: wes is no admin
		{"rae", "staging", false},    // read access only
		{"wes", "qa", false},         // no branch feeds qa
		{"root", "production", true}, // a varTrack admin
	} {
		c, err := a.ForDatasource(ctx, tc.token, "", "redis")
		if err != nil {
			t.Fatal(err)
		}
		ok, why, err := a.MaySync(ctx, tc.token, "redis", tc.env, c)
		if err != nil {
			t.Fatal(err)
		}
		if ok != tc.want {
			t.Errorf("%s → %s: may sync = %v (%s), want %v", tc.token, tc.env, ok, why, tc.want)
		}
	}
}

func TestARuleIsIdentifiedByItsPlatformAndDatasource(t *testing.T) {
	a, _, _ := newAuthz()
	ctx := context.Background()

	// Rules on two platforms write to mongo-primary: the platform must be named.
	_, err := a.ForDatasource(ctx, "wes", "", "mongo-primary")
	var ambiguous *auth.AmbiguousRuleError
	if !errors.As(err, &ambiguous) {
		t.Fatalf("no platform named: err = %v, want an *AmbiguousRuleError", err)
	}
	if got := ambiguous.Platforms; len(got) != 2 || got[0] != "gitea" || got[1] != "gitea-team" {
		t.Errorf("platforms offered = %v", got)
	}
	if msg := ambiguous.Error(); msg != "datasource mongo-primary has rules on gitea, gitea-team; say which with --platform" {
		t.Errorf("message = %q", msg)
	}

	// Named, each platform's own rule decides, and the token is asked of that platform.
	c, err := a.ForDatasource(ctx, "wes", "gitea", "mongo-primary")
	if err != nil || c.Platform != "gitea" || c.PermissionName() != "write" {
		t.Errorf("wes on gitea = %+v, %v", c, err)
	}
	c, err = a.ForDatasource(ctx, "tia", "gitea-team", "mongo-primary")
	if err != nil || c.Platform != "gitea-team" || c.PermissionName() != "write" {
		t.Errorf("tia on gitea-team = %+v, %v", c, err)
	}
	if _, err = a.ForDatasource(ctx, "tia", "gitea", "mongo-primary"); !errors.Is(err, models.ErrTokenRejected) {
		t.Errorf("a Gitea-team token on the Gitea rule: err = %v, want the token rejected", err)
	}
	if _, err = a.ForDatasource(ctx, "wes", "github", "mongo-primary"); !errors.Is(err, auth.ErrNoRule) {
		t.Errorf("a platform with no rule for the datasource: err = %v", err)
	}
	// A datasource with one rule needs no platform.
	if c, err = a.ForDatasource(ctx, "tia", "", "redis-team"); err != nil || c.Platform != "gitea-team" {
		t.Errorf("one rule, no platform named: %+v, %v", c, err)
	}
}

func TestASyncIsCheckedOnTheCallersOwnRule(t *testing.T) {
	a, _, _ := newAuthz()
	ctx := context.Background()
	c, err := a.ForDatasource(ctx, "tia", "gitea-team", "mongo-primary")
	if err != nil {
		t.Fatal(err)
	}
	ok, why, err := a.MaySync(ctx, "tia", "mongo-primary", "staging", c)
	if err != nil || !ok {
		t.Errorf("tia may push to payments/config develop: ok=%v why=%q err=%v", ok, why, err)
	}
}
