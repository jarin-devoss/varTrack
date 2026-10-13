package config

import (
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"
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
