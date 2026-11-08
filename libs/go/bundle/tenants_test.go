package bundle

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func loadCue(t *testing.T, body string) error {
	t.Helper()
	file := filepath.Join(t.TempDir(), "config.cue")
	if err := os.WriteFile(file, []byte("bundle: {\n"+body+"\n}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, strict := range []bool{true, false} { // every service reads tenants the same way
		if _, err := Load(file, Options{Strict: strict, Fragment: true}); err != nil {
			return err
		}
	}
	return nil
}

func TestDeclaredTenants(t *testing.T) {
	rule := `rules: [{platform: "gitea", datasource: "mongo", file_name: "app.yaml", tenants: ["payments"]}]`
	tests := []struct {
		name string
		cue  string
		want string // empty: accepted
	}{
		{"none declared", rule, ""},
		{"a rule can name any tenant while none are declared", `rules: [{platform: "gitea", datasource: "mongo", file_name: "a.yaml", tenants: ["whoever"]}]`, ""},
		{"two disjoint tenants", `tenants: [
			{name: "payments", repositories: ["pay/*"], destinations: [{datasource: "mongo", paths: ["pay_*"]}]},
			{name: "search", repositories: ["search/*"], destinations: [{datasource: "mongo", paths: ["search_*"]}]},
		]
		` + rule, ""},
		{"a name that is not a tenant name", `tenants: [{name: "a b"}]`, `name "a b" is not a tenant name`},
		{"a name with a query in it", `tenants: [{name: "demo&limit=1"}]`, "is not a tenant name"},
		{"an empty name", `tenants: [{}]`, "is not a tenant name"},
		{"the same name twice", `tenants: [{name: "a"}, {name: "a"}]`, `repeats the name "a"`},
		{"one repository in two tenants", `tenants: [{name: "a", repositories: ["org/app"]}, {name: "b", repositories: ["org/app"]}]`,
			`of tenant "a"`},
		{"a repository inside another tenant's wildcard", `tenants: [{name: "a", repositories: ["org/*"]}, {name: "b", repositories: ["Org/App"]}]`,
			"no repository may belong to two tenants"},
		{"one schema repository for two tenants", `tenants: [
			{name: "a", schema_repo: {platform: "gitea", repo: "org/schemas"}},
			{name: "b", schema_repo: {platform: "gitea", repo: "org/schemas.git"}},
		]`, "each tenant needs its own"},
		{"a schema repository without a repo", `tenants: [{name: "a", schema_repo: {platform: "gitea"}}]`, "without a repo"},
		{"the same place on one datasource", `tenants: [
			{name: "a", destinations: [{datasource: "linux", paths: ["/etc/app/*"]}]},
			{name: "b", destinations: [{datasource: "linux", paths: ["/etc/app/b/x.env"]}]},
		]`, "could write the same place"},
		{"the same pattern on different datasources", `tenants: [
			{name: "a", destinations: [{datasource: "linux", paths: ["/etc/app/*"]}]},
			{name: "b", destinations: [{datasource: "redis", paths: ["/etc/app/*"]}]},
		]`, ""},
		{"a destination without paths", `tenants: [{name: "a", destinations: [{datasource: "linux"}]}]`, "without a datasource or without paths"},
		{"a rule naming a tenant nobody declared", `tenants: [{name: "search"}]
		` + rule, `names the tenant "payments"`},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := loadCue(t, tc.cue)
			switch {
			case tc.want == "" && err != nil:
				t.Fatalf("unexpected error: %v", err)
			case tc.want != "" && (err == nil || !strings.Contains(err.Error(), tc.want)):
				t.Fatalf("error = %v, want it to mention %q", err, tc.want)
			}
		})
	}
}

func TestTenantsReachTheExportedBundle(t *testing.T) {
	file := filepath.Join(t.TempDir(), "config.cue")
	cue := `bundle: {tenants: [{name: "payments", repositories: ["pay/*"],
		schema_repo: {platform: "gitea", repo: "pay/schemas", branch: "main", token: "s3cret"}}]}`
	if err := os.WriteFile(file, []byte(cue), 0o600); err != nil {
		t.Fatal(err)
	}
	b, err := Load(file, Options{Strict: true, Fragment: true})
	if err != nil {
		t.Fatal(err)
	}
	if got := b.GetTenants()[0].GetSchemaRepo().GetToken().GetValue(); got != "s3cret" {
		t.Fatalf("schema_repo.token = %q, want the shorthand expanded to a SecretRef", got)
	}
	out, err := export(file, Options{Strict: true, Fragment: true})
	if err != nil || !strings.Contains(string(out), `"tenants"`) {
		t.Fatalf("Export = %s, %v; want the tenants in it", out, err)
	}
}
