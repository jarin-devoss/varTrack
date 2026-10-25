package bundle

import (
	"fmt"
	"path/filepath"
	"testing"
)

// TestEveryExampleBundleLoads keeps examples/bundles loadable. A bundle the
// gateway rejects stops anyone who copies it at startup, and the gateway is
// the strict reader, so the examples are loaded strictly.
func TestEveryExampleBundleLoads(t *testing.T) {
	files, _ := filepath.Glob("../../../examples/bundles/*.cue")
	if len(files) == 0 {
		t.Skip("examples not available")
	}
	for _, f := range files {
		for _, strict := range []bool{true, false} {
			t.Run(fmt.Sprintf("%s/strict=%v", filepath.Base(f), strict), func(t *testing.T) {
				if _, err := Load(f, Options{Strict: strict}); err != nil {
					t.Errorf("Load(strict=%v): %v", strict, err)
				}
			})
		}
	}
}

// TestTheDemoStacksConfigsLoad: the e2e and live demo stacks run the gateway on
// these, so a setting the proto does not declare stops the stack at startup.
func TestTheDemoStacksConfigsLoad(t *testing.T) {
	for _, f := range []string{"../../../e2e/config.cue", "../../../e2e/live/config.cue"} {
		t.Run(filepath.ToSlash(f), func(t *testing.T) {
			b, err := Load(f, Options{Strict: true})
			if err != nil {
				t.Fatal(err)
			}
			if len(b.GetDatasources()) == 0 {
				t.Error("no datasources loaded")
			}
		})
	}
}

func TestTheDemoStackHasAVaultWithAPathPrefix(t *testing.T) {
	b, err := Load("../../../e2e/config.cue", Options{Strict: true})
	if err != nil {
		t.Fatal(err)
	}
	managers := b.GetSecretManagers()
	if len(managers) != 1 || managers[0].GetVault().GetPathPrefix() != "vartrack/configs" {
		t.Errorf("secret managers = %v, want one Vault with path_prefix vartrack/configs", managers)
	}
}
