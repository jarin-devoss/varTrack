package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The gateway reads config.cue strictly: a setting the proto does not declare
// stops it at startup, so a typo is found then and not when a webhook needs it.
// (The watcher reads the same file leniently; libs/go/bundle tests both.)
func TestTheGatewayReadsTheBundleStrictly(t *testing.T) {
	file := filepath.Join(t.TempDir(), "config.cue")
	cue := `bundle: {rules: [{platform: "github", datasource: "mongo", not_a_setting: true}]}` + "\n"
	if err := os.WriteFile(file, []byte(cue), 0o600); err != nil {
		t.Fatal(err)
	}

	_, err := NewBundle(file)

	if err == nil || !strings.Contains(err.Error(), "not_a_setting") {
		t.Fatalf("NewBundle error = %v, want the unknown setting named", err)
	}
}

func TestTheGatewayLoadsAnExampleBundle(t *testing.T) {
	files, _ := filepath.Glob("../../../examples/bundles/*.cue")
	if len(files) == 0 {
		t.Skip("examples not available")
	}
	b, err := NewBundle(files[0])
	if err != nil {
		t.Fatal(err)
	}
	if b == nil {
		t.Fatal("no bundle")
	}
}
