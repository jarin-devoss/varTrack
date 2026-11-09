package bundle

import (
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

func TestOldOptionNamesStillLoad(t *testing.T) {
	// A bundle written before the rename: the gateway parses strictly, so the
	// old names must be moved to the new fields before unmarshaling.
	in := []byte(`{
		"rules": [
			{"platform": "gitea", "datasource": "mongo", "env_as_branch": true, "env_as_pr": true, "env_as_tags": true},
			{"platform": "gitea", "datasource": "redis", "env_as_pr": true, "pr_as_env": false}
		],
		"datasources": [{"s3": {"env_as_prefix": true}}]
	}`)
	normalized, err := normalizeSecretRefs(in)
	if err != nil {
		t.Fatal(err)
	}
	b := &pb_models.Bundle{}
	if err := protojson.Unmarshal(normalized, b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	old := b.GetRules()[0]
	if !old.GetBranchAsEnv() || !old.GetPrAsEnv() || !old.GetTagAsEnv() {
		t.Errorf("old names not carried over: %v", old)
	}
	if b.GetRules()[1].GetPrAsEnv() {
		t.Error("when both names are set, the current name should win")
	}
	if !b.GetDatasources()[0].GetS3().GetPrefixAsEnv() {
		t.Error("s3.env_as_prefix not carried over to prefix_as_env")
	}
}

func TestRemovedPruneLastStillLoads(t *testing.T) {
	// prune.last was removed from the proto; the strict parse would reject it.
	in := []byte(`{
		"rules": [
			{"platform": "gitea", "datasource": "mongo", "prune": {"enabled": true, "last": true, "protection": ["a.*"]}},
			{"platform": "gitea", "datasource": "redis", "prune": {"last": true}}
		]
	}`)
	normalized, err := normalizeSecretRefs(in)
	if err != nil {
		t.Fatal(err)
	}
	b := &pb_models.Bundle{}
	if err := protojson.Unmarshal(normalized, b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	rules := b.GetRules()
	if p := rules[0].GetPrune(); !p.GetEnabled() || len(p.GetProtection()) != 1 {
		t.Errorf("the rest of the block should be kept: %v", p)
	}
	if rules[1].GetPrune().GetEnabled() {
		t.Error("a prune block with only last is a disabled block")
	}
}

func TestDropRemovedOptionsNamesWhatItDropped(t *testing.T) {
	bundle := map[string]interface{}{"rules": []interface{}{
		map[string]interface{}{"prune": map[string]interface{}{"last": false}},
		map[string]interface{}{"prune": true},
		map[string]interface{}{},
	}}
	got := dropRemovedOptions(bundle)
	if len(got) != 1 || got[0] != "rules[0].prune.last" {
		t.Errorf("dropped = %v", got)
	}
}

func TestTheRenamedOptionsComeFromTheProtos(t *testing.T) {
	// The old names are stated on the options in the protos, not in a table
	// here. These are the five there are; a new rename adds its old_names to the
	// option, and this list.
	wantRule := map[string]string{
		"env_as_branch": "branch_as_env", "env_as_pr": "pr_as_env", "env_as_tags": "tag_as_env",
		"zk_path_template": "destination_template",
	}
	wantS3 := map[string]string{"env_as_prefix": "prefix_as_env"}
	for name, pair := range map[string][2]map[string]string{
		"rule": {renamedRuleOptions, wantRule},
		"s3":   {renamedS3Options, wantS3},
	} {
		got, want := pair[0], pair[1]
		if len(got) != len(want) {
			t.Errorf("%s: renamed = %v, want %v", name, got, want)
		}
		for old, current := range want {
			if got[old] != current {
				t.Errorf("%s: %s → %q, want %q", name, old, got[old], current)
			}
		}
	}
}

func TestRemovedOptionsLoadUnderTheStrictParse(t *testing.T) {
	// Each of these was a proto field or option that nothing read. A config.cue
	// that still sets one must keep loading: the strict parse would stop the
	// gateway on an unknown field.
	in := []byte(`{
		"watcher_state_datasource": "redis-broker",
		"rules": [
			{"platform": "gitea", "datasource": "mongo", "retry_backoff": "30s", "prune": {"enabled": true, "last": true}}
		]
	}`)
	b := parse(t, string(in), true)
	if rule := b.GetRules()[0]; !rule.GetPrune().GetEnabled() {
		t.Errorf("the rest of the rule should load: %v", rule)
	}
}

func TestDropRemovedOptionsSaysWhatItDroppedAndWhy(t *testing.T) {
	bundle := map[string]interface{}{
		"watcher_state_datasource": "redis",
		"rules": []interface{}{
			map[string]interface{}{"retry_backoff": "1s", "prune": map[string]interface{}{"last": true}},
			map[string]interface{}{"prune": true},
		},
	}
	got := dropRemovedOptions(bundle)
	want := []string{"rules[0].prune.last", "rules[0].retry_backoff", "watcher_state_datasource"}
	if len(got) != len(want) {
		t.Fatalf("dropped = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("dropped[%d] = %q, want %q", i, got[i], want[i])
		}
	}
	if _, still := bundle["watcher_state_datasource"]; still {
		t.Error("watcher_state_datasource is still in the bundle")
	}
}

func TestATenantIDInAConfigCueRuleIsDroppedNotRefused(t *testing.T) {
	// The tenant is where a file lives, never something it says: tenant_id is no rule option. A config.cue
	// that still writes it must load (the gateway and the watcher would not start otherwise), without the key.
	in := []byte(`{
		"rules": [
			{"platform": "gitea", "datasource": "mongo", "tenant_id": "acme", "prune": {"enabled": true}},
			{"platform": "gitea", "datasource": "redis"}
		]
	}`)
	normalized, err := normalizeSecretRefs(in)
	if err != nil {
		t.Fatal(err)
	}
	b := &pb_models.Bundle{}
	if err := protojson.Unmarshal(normalized, b); err != nil {
		t.Fatalf("a rule with the removed tenant_id was refused: %v", err)
	}
	if rules := b.GetRules(); len(rules) != 2 || !rules[0].GetPrune().GetEnabled() {
		t.Errorf("the rest of the rule should be kept: %v", rules)
	}
	if _, found := removedRuleOptions["tenant_id"]; !found {
		t.Error("tenant_id is not listed as a removed rule option")
	}
}

func TestARuleThatSetDryRunStillLoadsAndWrites(t *testing.T) {
	// dry_run (the rule's, and prune's) was removed from the proto: a dry run is started, not configured.
	in := []byte(`{
		"rules": [
			{"platform": "gitea", "datasource": "mongo", "dry_run": true, "prune": {"enabled": true, "dry_run": true, "protection": ["a.*"]}},
			{"platform": "gitea", "datasource": "redis", "prune": {"dry_run": true}}
		]
	}`)
	normalized, err := normalizeSecretRefs(in)
	if err != nil {
		t.Fatal(err)
	}
	b := &pb_models.Bundle{}
	if err := protojson.Unmarshal(normalized, b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	rules := b.GetRules()
	if p := rules[0].GetPrune(); !p.GetEnabled() || len(p.GetProtection()) != 1 {
		t.Errorf("the rest of the prune block should be kept: %v", p)
	}
	if rules[1].GetPrune().GetEnabled() {
		t.Error("a prune block with only dry_run is a disabled block")
	}
}

func TestDropRemovedOptionsNamesTheDryRunsAndSaysWhatToDoInstead(t *testing.T) {
	bundle := map[string]interface{}{"rules": []interface{}{
		map[string]interface{}{"dry_run": true, "prune": map[string]interface{}{"enabled": true, "dry_run": true}},
	}}
	got := dropRemovedOptions(bundle)
	if len(got) != 2 || got[0] != "rules[0].dry_run" || got[1] != "rules[0].prune.dry_run" {
		t.Fatalf("dropped = %v", got)
	}
	rule := bundle["rules"].([]interface{})[0].(map[string]interface{})
	if _, ok := rule["dry_run"]; ok {
		t.Error("dry_run was not dropped")
	}
	if _, ok := rule["prune"].(map[string]interface{})["dry_run"]; ok {
		t.Error("prune.dry_run was not dropped")
	}
}

func TestAMaxChangedFilesInARuleIsDroppedNotRefused(t *testing.T) {
	// It cut the "every file the push changed" fallback of a rule that named no file; a rule now reads
	// only the file it names, so a config that still sets it must load, and be told it does nothing.
	bundle := map[string]interface{}{"rules": []interface{}{
		map[string]interface{}{"platform": "gitea", "datasource": "mongo", "file_name": "app.yaml", "max_changed_files": 50},
	}}
	if got := dropRemovedOptions(bundle); len(got) != 1 || got[0] != "rules[0].max_changed_files" {
		t.Fatalf("dropped = %v", got)
	}
	if _, ok := bundle["rules"].([]interface{})[0].(map[string]interface{})["max_changed_files"]; ok {
		t.Error("max_changed_files was not dropped")
	}
	b := parse(t, `{"rules": [{"platform": "gitea", "datasource": "mongo", "file_name": "app.yaml", "max_changed_files": 50}]}`, true)
	if b.GetRules()[0].GetFileName() != "app.yaml" {
		t.Errorf("the rest of the rule should load: %v", b.GetRules()[0])
	}
}
