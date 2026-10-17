package config

import (
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
)

func TestOldOptionNamesAreNotDroppedSilently(t *testing.T) {
	// The watcher discards unknown fields: without the rename, a bundle
	// written with the old names would load with every strategy off.
	in := []byte(`{
		"rules": [{"platform": "gitea", "datasource": "mongo", "self_heal": true, "env_as_branch": true, "env_as_pr": true, "env_as_tags": true}],
		"datasources": [{"s3": {"env_as_prefix": true}}]
	}`)
	b := &models.Bundle{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(normalizeSecretRefs(in), b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	r := b.GetRules()[0]
	if !r.GetBranchAsEnv() || !r.GetPrAsEnv() || !r.GetTagAsEnv() {
		t.Errorf("old names dropped: %v", r)
	}
	if !b.GetDatasources()[0].GetS3().GetPrefixAsEnv() {
		t.Error("s3.env_as_prefix dropped")
	}
}
