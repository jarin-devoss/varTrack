package bundle

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestExportRefusesWhatTheStrictLoaderRefuses(t *testing.T) {
	file := writeCUE(t, `bundle: {
	rules: [{platform: "github", datasource: "mongo", not_a_setting: true}]
}
`)
	if _, err := export(file, Options{Strict: true, Fragment: true}); err == nil || !strings.Contains(err.Error(), "not_a_setting") {
		t.Errorf("err = %v, want the unknown field named", err)
	}
}

func TestExportHandsOverTheBundleAsTheProtoReadsIt(t *testing.T) {
	file := writeCUE(t, `bundle: {
	platforms: [{gitea: {endpoint: "http://gitea:3000", token: "abc"}}]
	rules: [{platform: "gitea", datasource: "mongo", repositories: ["acme/configs"], env_as_branch: true, sync_mode: "AUTO"}]
}
`)
	out, err := export(file, Options{Strict: true, Fragment: true})
	if err != nil {
		t.Fatal(err)
	}
	var got struct {
		Platforms []map[string]map[string]interface{} `json:"platforms"`
		Rules     []map[string]interface{}            `json:"rules"`
	}
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	rule := got.Rules[0]
	if _, old := rule["env_as_branch"]; old || rule["branch_as_env"] != true {
		t.Errorf("a renamed option was not moved to its current name: %v", rule)
	}
	if rule["sync_mode"] != "SYNC_MODE_AUTO" {
		t.Errorf("sync_mode = %v, want its proto name", rule["sync_mode"])
	}
	token, _ := got.Platforms[0]["gitea"]["token"].(map[string]interface{})
	if token["value"] != "abc" {
		t.Errorf("a credential written as a string is not a SecretRef: %v", got.Platforms[0]["gitea"]["token"])
	}
}

func TestPruneTrueIsTheShorthandForAnEnabledBlock(t *testing.T) {
	file := writeCUE(t, `bundle: {
	rules: [
		{platform: "github", datasource: "mongo", prune: true},
		{platform: "github", datasource: "redis", prune: false},
		{platform: "github", datasource: "s3", prune: {protection: ["keep.*"]}},
	]
}
`)
	b, err := Load(file, Options{Strict: true, Fragment: true})
	if err != nil {
		t.Fatal(err)
	}
	rules := b.GetRules()
	if !rules[0].GetPrune().GetEnabled() || rules[1].GetPrune().GetEnabled() || rules[2].GetPrune().GetEnabled() {
		t.Errorf("prune = %v, %v, %v", rules[0].GetPrune(), rules[1].GetPrune(), rules[2].GetPrune())
	}
}

func TestDurationsCanBeWrittenTheWayTheDocsWriteThem(t *testing.T) {
	file := writeCUE(t, `bundle: {
	datasources: [{helm: {namespace: "ns", release_name: "r", wait_timeout: "5m", timeout: "1h30m"}}]
	secret_managers: [{vault: {endpoint: "https://v", token_auth: {token: "t"}, timeout: "1500ms"}}]
}
`)
	b, err := Load(file, Options{Strict: true, Fragment: true})
	if err != nil {
		t.Fatal(err)
	}
	h := b.GetDatasources()[0].GetHelm()
	if h.GetWaitTimeout().AsDuration().Minutes() != 5 || h.GetTimeout().AsDuration().Minutes() != 90 {
		t.Errorf("helm durations = %v, %v", h.GetWaitTimeout(), h.GetTimeout())
	}
	if got := b.GetSecretManagers()[0].GetVault().GetTimeout().AsDuration().Milliseconds(); got != 1500 {
		t.Errorf("vault timeout = %dms", got)
	}
}
