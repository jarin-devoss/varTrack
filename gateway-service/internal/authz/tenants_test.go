package authz_test

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"gateway-service/internal/authz"
	"gateway-service/internal/models"
	pb "vartrack/libs/gen/vartrack/v1/models"
)

// tenantRules is a bundle that declares tenants.
type tenantRules struct {
	fakeRules
	tenants []*pb.Tenant
}

func (t tenantRules) Tenants() []*pb.Tenant { return t.tenants }

// A rule covering the repositories of two tenants: acme-team's acme/configs and web-team's
// acme/web-app. wes can reach the first, gus the second, ann both, root is an admin.
func tenantWorld(t *testing.T, ruleTenants []string, declared ...*pb.Tenant) (*authz.Engine, tenantRules) {
	t.Helper()
	gitea := &fakeGit{perms: map[string]map[string]models.RepoPermission{
		"wes":  {"acme/configs": write},
		"gus":  {"acme/web-app": read},
		"ann":  {"acme/configs": write, "acme/web-app": read},
		"zed":  {"other/thing": write},
		"root": {},
	}}
	rules := tenantRules{
		fakeRules: fakeRules{
			rules: []*pb.Rule{
				{Platform: "gitea", Datasource: "redis", Repositories: []string{"acme/configs", "acme/web-app"},
					BranchMap: map[string]string{"develop": "staging"}, Tenants: ruleTenants},
			},
			platforms: map[string]*fakeGit{"gitea": gitea},
			names:     []string{"gitea"},
		},
		tenants: declared,
	}
	return authz.New(rules, []string{"gitea:root"}), rules
}

var (
	acmeTeam = &pb.Tenant{Name: "acme-team", Repositories: []string{"acme/configs"}}
	webTeam  = &pb.Tenant{Name: "web-team", Repositories: []string{"acme/*-app"}}
)

func decideRead(t *testing.T, e *authz.Engine, token, tenant string) (*authz.Decision, error) {
	t.Helper()
	return e.Decide(context.Background(), authz.Request{
		Identities: []authz.Identity{{Token: token}}, Action: authz.Read,
		RulePlatform: "gitea", RuleDatasource: "redis", TenantID: tenant,
	})
}

func TestWithNoTenantsDeclaredThereIsTheImplicitDefault(t *testing.T) {
	e, _ := tenantWorld(t, nil)
	d, err := decideRead(t, e, "wes", "")
	if err != nil || !d.Allowed || d.Tenant != "default" {
		t.Fatalf("got %+v, %v; want allowed for tenant default", d, err)
	}
	if d, _ = decideRead(t, e, "wes", "anything"); d.Tenant != "anything" {
		t.Fatalf("a name asked for with none declared stays, got %q", d.Tenant)
	}
}

func TestACallerActsOnlyForATenantTheirGitAccessReaches(t *testing.T) {
	e, _ := tenantWorld(t, nil, acmeTeam, webTeam)

	d, err := decideRead(t, e, "wes", "acme-team")
	if err != nil || !d.Allowed || d.Tenant != "acme-team" {
		t.Fatalf("wes for acme-team: %+v, %v", d, err)
	}
	// wes can read acme/configs, which is acme-team's; web-team's repository is out of reach.
	d, err = decideRead(t, e, "wes", "web-team")
	if err != nil || d.Allowed || d.DeniedBy != authz.DeniedByTenant || !strings.Contains(d.Reason, "web-team") {
		t.Fatalf("wes for web-team: %+v, %v; want a denial by tenant", d, err)
	}
	// A tenant nobody declared is not one, however it is spelled.
	d, err = decideRead(t, e, "wes", "evil")
	if err != nil || d.Allowed || !strings.Contains(d.Reason, "acme-team, web-team") {
		t.Fatalf("wes for evil: %+v, %v; want the declared tenants named", d, err)
	}
	// A caller with access to neither reaches no tenant.
	if d, _ = decideRead(t, e, "zed", "acme-team"); d.Allowed {
		t.Fatalf("zed has no access to acme/configs: %+v", d)
	}
}

func TestNoTenantNamedTakesTheOneTheCallerReaches(t *testing.T) {
	e, _ := tenantWorld(t, nil, acmeTeam, webTeam)
	for token, want := range map[string]string{"wes": "acme-team", "gus": "web-team"} {
		d, err := decideRead(t, e, token, "")
		if err != nil || !d.Allowed || d.Tenant != want {
			t.Fatalf("%s: %+v, %v; want %s", token, d, err, want)
		}
	}
	for _, token := range []string{"ann", "root"} {
		_, err := decideRead(t, e, token, "")
		var ambiguous *authz.AmbiguousTenantError
		if !errors.As(err, &ambiguous) || !reflect.DeepEqual(ambiguous.Tenants, []string{"acme-team", "web-team"}) {
			t.Fatalf("%s: err = %v; want the two tenants to choose from", token, err)
		}
	}
	if d, _ := decideRead(t, e, "root", "web-team"); !d.Allowed || d.Tenant != "web-team" {
		t.Fatalf("an admin may name any declared tenant: %+v", d)
	}
}

func TestARuleNamesTheTenantsItAllows(t *testing.T) {
	e, _ := tenantWorld(t, []string{"web-team"}, acmeTeam, webTeam)
	d, _ := decideRead(t, e, "wes", "acme-team")
	if d.Allowed || !strings.Contains(d.Reason, "not one of the tenants of the redis rule") {
		t.Fatalf("the rule leaves acme-team out: %+v", d)
	}
	if d, _ = decideRead(t, e, "ann", ""); !d.Allowed || d.Tenant != "web-team" {
		t.Fatalf("ann has one tenant to act for in this rule: %+v", d)
	}
}

func TestATenantWithNoRepositoriesIsReachedOnlyAsTheRulesOwnWhenNobodyOwnsItsRepositories(t *testing.T) {
	bare := &pb.Tenant{Name: "bare"}
	// The rule's repositories belong to acme-team: bare has nothing Git can vouch for.
	e, _ := tenantWorld(t, []string{"bare"}, acmeTeam, bare)
	if d, _ := decideRead(t, e, "wes", "bare"); d.Allowed {
		t.Fatalf("Git cannot say wes may act for a tenant with no repositories: %+v", d)
	}
	// No declared tenant owns the rule's repositories and bare is its one tenant: Git is asked
	// about the rule's repositories as they are.
	e, _ = tenantWorld(t, []string{"bare"}, &pb.Tenant{Name: "elsewhere", Repositories: []string{"nobody/home"}}, bare)
	if d, _ := decideRead(t, e, "wes", ""); !d.Allowed || d.Tenant != "bare" {
		t.Fatalf("wes for the rule's only tenant: %+v", d)
	}
}

func TestTenantsForAndResolveTenant(t *testing.T) {
	e, rules := tenantWorld(t, nil, acmeTeam, webTeam)
	rule, err := e.ResolveRule("gitea", "redis")
	if err != nil {
		t.Fatal(err)
	}
	ids := func(token string) []authz.Identity { return []authz.Identity{{Token: token}} }

	got, err := e.TenantsFor(context.Background(), ids("ann"), rule)
	if err != nil || !reflect.DeepEqual(got, []string{"acme-team", "web-team"}) {
		t.Fatalf("TenantsFor(ann) = %v, %v", got, err)
	}
	if got, _ = e.TenantsFor(context.Background(), ids("gus"), rule); !reflect.DeepEqual(got, []string{"web-team"}) {
		t.Fatalf("TenantsFor(gus) = %v", got)
	}
	if got, _ = e.TenantsFor(context.Background(), ids("zed"), rule); len(got) != 0 {
		t.Fatalf("TenantsFor(zed) = %v, want none", got)
	}

	if tenant, err := e.ResolveTenant(context.Background(), ids("wes"), ""); err != nil || tenant != "acme-team" {
		t.Fatalf("ResolveTenant(wes) = %q, %v", tenant, err)
	}
	if _, err := e.ResolveTenant(context.Background(), ids("wes"), "web-team"); err == nil {
		t.Fatal("wes cannot act for web-team")
	}
	if _, err := e.ResolveTenant(context.Background(), ids("wes"), "evil"); err == nil || !strings.Contains(err.Error(), "not declared") {
		t.Fatalf("an undeclared tenant: %v", err)
	}
	var ambiguous *authz.AmbiguousTenantError
	if _, err := e.ResolveTenant(context.Background(), ids("ann"), ""); !errors.As(err, &ambiguous) {
		t.Fatalf("ann reaches two tenants: %v", err)
	}
	if _, err := e.ResolveTenant(context.Background(), ids("nobody-at-all"), ""); !errors.Is(err, models.ErrTokenRejected) {
		t.Fatalf("a rejected token stays rejected: %v", err)
	}

	// With none declared every caller has the implicit one.
	plain := authz.New(rules.fakeRules, nil)
	if tenant, err := plain.ResolveTenant(context.Background(), ids("wes"), ""); err != nil || tenant != "default" {
		t.Fatalf("ResolveTenant with none declared = %q, %v", tenant, err)
	}
}

func TestReadableRulesAreThoseWithinReachOfTheTenant(t *testing.T) {
	e, _ := tenantWorld(t, nil, acmeTeam, webTeam)
	rules := e.BundleRules()
	ids := []authz.Identity{{Token: "gus"}}

	got, err := e.ReadableRules(context.Background(), ids, rules, "web-team")
	if err != nil || len(got.Rules) != 1 {
		t.Fatalf("gus for web-team: %+v, %v", got, err)
	}
	if got, _ = e.ReadableRules(context.Background(), ids, rules, "acme-team"); len(got.Rules) != 0 {
		t.Fatalf("gus cannot reach acme-team's repositories: %+v", got)
	}
	if got, _ = e.ReadableRules(context.Background(), ids, rules, ""); len(got.Rules) != 1 {
		t.Fatalf("with no tenant named, a rule within reach of one is listed: %+v", got)
	}
}
