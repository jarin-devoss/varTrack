package bundle

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func loadRules(t *testing.T, rule string) error {
	t.Helper()
	file := filepath.Join(t.TempDir(), "config.cue")
	body := "bundle: {\n\tplatforms: [{gitea: {endpoint: \"http://gitea:3000\"}}]\n" +
		"\tdatasources: [{mongo: {endpoint: \"mongodb://mongo:27017\", database: \"v\", collection: \"c\"}}]\n" +
		"\trules: [{platform: \"gitea\", datasource: \"mongo\", repositories: [\"acme/configs\"], " + rule + "}]\n}\n"
	if err := os.WriteFile(file, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := Load(file, Options{Strict: true})
	return err
}

func TestASchemaMapOfGoodEntriesLoads(t *testing.T) {
	err := loadRules(t, `schema_map: [
		{match: "services/api/**/*.yaml", schema: "api/settings.cue"},
		{match: ".env", schema: "env.cue"},
		{match: "legacy/**", schema: "none"},
		{match: "app-[ab].yaml", schema: "app.cue"},
	], schema_from_file: true`)
	if err != nil {
		t.Fatalf("a valid schema_map was refused: %v", err)
	}
}

func TestEveryBadEntryIsNamedInOneError(t *testing.T) {
	err := loadRules(t, `schema_map: [
		{match: "", schema: "a.cue"},
		{match: "a/**", schema: "../outside.cue"},
		{match: "b\\c", schema: "/etc/passwd.cue"},
		{match: "x[", schema: "d:/x.cue"},
		{match: "ok/*.yaml", schema: "no-extension"},
		{match: "ok/*.json", schema: "a/./b//c.cue"},
	]`)
	if err == nil {
		t.Fatal("bad entries loaded")
	}
	for _, want := range []string{
		"schema_map[0].match", "is empty",
		"schema_map[1].schema", `".." segment`,
		"schema_map[2].match", "backslash", "schema_map[2].schema", "absolute",
		"schema_map[3].match", `no closing`, "schema_map[3].schema",
		"schema_map[4].schema", `does not end in ".cue"`,
		"schema_map[5].schema", "empty segment",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not say %q:\n%v", want, err)
		}
	}
}

func TestGlobAndSchemaProblems(t *testing.T) {
	for pattern, bad := range map[string]bool{
		"*.yaml": false, "**": false, "a/**/b?.json": false, "[abc].yaml": false,
		"": true, "/abs": true, `a\b`: true, "a[": true, "a[]": true, "a***": true,
	} {
		if got := globProblem(pattern) != ""; got != bad {
			t.Errorf("globProblem(%q) problem=%v, want %v", pattern, got, bad)
		}
	}
	for schema, bad := range map[string]bool{
		"none": false, "a.cue": false, "dir/a.cue": false,
		"": true, "a": true, "../a.cue": true, "a/../b.cue": true, "/a.cue": true, `a\b.cue`: true, "C:/a.cue": true,
	} {
		if got := schemaProblem(schema) != ""; got != bad {
			t.Errorf("schemaProblem(%q) problem=%v, want %v", schema, got, bad)
		}
	}
}
