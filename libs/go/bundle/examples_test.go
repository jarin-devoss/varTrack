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

// TestTheExampleBundlesNameOnlyWhatTheyDeclare: the loader does not refuse a rule, or a Celery or
// replay-store setting, that names a datasource the bundle does not have, and it keeps any
// global_tags key. An example that did so would load and then write to a sink's defaults, or wire
// nothing, for whoever copied it.
func TestTheExampleBundlesNameOnlyWhatTheyDeclare(t *testing.T) {
	files, _ := filepath.Glob("../../../examples/bundles/*.cue")
	if len(files) == 0 {
		t.Skip("examples not available")
	}
	readTags := map[string]bool{"git_cache_dir": true, "git_cache_max_entries": true, "schema_cache_dir": true, "schema_ttl": true}
	for _, f := range files {
		t.Run(filepath.Base(f), func(t *testing.T) {
			b, err := Load(f, Options{Strict: true})
			if err != nil {
				t.Fatal(err)
			}
			declared := datasourceNames(b)
			named := map[string]string{
				"celery_broker_datasource":           b.GetCeleryBrokerDatasource(),
				"celery_backend_datasource":          b.GetCeleryBackendDatasource(),
				"gateway_nonce_datasource":           b.GetGatewayNonceDatasource(),
				"watcher_leader_election_datasource": b.GetWatcherLeaderElectionDatasource(),
			}
			for i, r := range b.GetRules() {
				named[fmt.Sprintf("rules[%d].datasource", i)] = r.GetDatasource()
			}
			for where, name := range named {
				if name != "" && !declared[name] {
					t.Errorf("%s = %q, which the bundle does not declare", where, name)
				}
			}
			for key := range b.GetGlobalTags() {
				if !readTags[key] {
					t.Errorf("global_tags.%s is read by nothing", key)
				}
			}
		})
	}
}

// TestTheDemoStacksConfigsLoad: the demo, the live demo and the e2e stacks run the gateway on
// these, so a setting the proto does not declare stops the stack at startup.
func TestTheDemoStacksConfigsLoad(t *testing.T) {
	for _, f := range []string{"../../../demo/config.cue", "../../../demo/live/config.cue", "../../../e2e/config.cue"} {
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
	b, err := Load("../../../demo/config.cue", Options{Strict: true})
	if err != nil {
		t.Fatal(err)
	}
	managers := b.GetSecretManagers()
	if len(managers) != 1 || managers[0].GetVault().GetPathPrefix() != "vartrack/configs" {
		t.Errorf("secret managers = %v, want one Vault with path_prefix vartrack/configs", managers)
	}
}
