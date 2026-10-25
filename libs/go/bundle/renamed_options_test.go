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
	// here. These are the four there are; a new rename adds its old_names to the
	// option, and this list.
	wantRule := map[string]string{"env_as_branch": "branch_as_env", "env_as_pr": "pr_as_env", "env_as_tags": "tag_as_env"}
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
