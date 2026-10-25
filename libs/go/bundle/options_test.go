package bundle

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeCUE(t *testing.T, body string) string {
	t.Helper()
	file := filepath.Join(t.TempDir(), "config.cue")
	if err := os.WriteFile(file, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return file
}

func TestStrictRejectsAFieldTheProtoDoesNotDeclareAndLenientIgnoresIt(t *testing.T) {
	// A typo in config.cue stops the gateway at startup; the watcher reads only
	// part of the bundle and must keep working when another service's settings change.
	file := writeCUE(t, `bundle: {
	rules: [{platform: "github", datasource: "mongo", not_a_setting: true}]
}
`)

	if _, err := Load(file, Options{Strict: true}); err == nil || !strings.Contains(err.Error(), "not_a_setting") {
		t.Errorf("strict: err = %v, want the unknown field named", err)
	}
	b, err := Load(file, Options{})
	if err != nil {
		t.Fatalf("lenient: %v", err)
	}
	if len(b.GetRules()) != 1 || b.GetRules()[0].GetDatasource() != "mongo" {
		t.Errorf("lenient: rules = %v", b.GetRules())
	}
}

func TestWhatIsNotACUEBundleIsRefusedEitherWay(t *testing.T) {
	for name, body := range map[string]string{
		"no bundle key": `other: {}`,
		"not concrete":  "bundle: {rules: [{platform: string, datasource: \"mongo\"}]}\n",
		"not CUE":       "bundle: {",
	} {
		file := writeCUE(t, body)
		for _, strict := range []bool{true, false} {
			if _, err := Load(file, Options{Strict: strict}); err == nil {
				t.Errorf("%s (strict=%v): loaded", name, strict)
			}
		}
	}
	if _, err := Load(filepath.Join(t.TempDir(), "missing.cue"), Options{}); err == nil {
		t.Error("a missing file loaded")
	}
}
