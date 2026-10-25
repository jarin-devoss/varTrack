package bundle

import (
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	enums "vartrack/libs/gen/vartrack/v1/enums"
	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

func TestSyncModeTakesTheDocumentedShortNames(t *testing.T) {
	in := []byte(`{"rules": [
		{"platform": "github", "datasource": "a", "sync_mode": "AUTO"},
		{"platform": "github", "datasource": "b", "sync_mode": "GIT_SMART_REPAIR"},
		{"platform": "github", "datasource": "c", "sync_mode": "SYNC_MODE_LIVE_STATE"},
		{"platform": "github", "datasource": "d", "sync_mode": "SYNC_MODE_FULL"},
		{"platform": "github", "datasource": "e"}
	]}`)
	normalized, err := normalizeSecretRefs(in)
	if err != nil {
		t.Fatal(err)
	}
	b := &pb_models.Bundle{}
	if err := protojson.Unmarshal(normalized, b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	want := []enums.SyncMode{
		enums.SyncMode_SYNC_MODE_AUTO,
		enums.SyncMode_SYNC_MODE_GIT_SMART_REPAIR,
		enums.SyncMode_SYNC_MODE_LIVE_STATE,
		enums.SyncMode_SYNC_MODE_GIT_UPSERT_ALL,
		enums.SyncMode_SYNC_MODE_UNSPECIFIED,
	}
	for i, r := range b.GetRules() {
		if r.GetSyncMode() != want[i] {
			t.Errorf("rule %s: sync_mode = %v, want %v", r.GetDatasource(), r.GetSyncMode(), want[i])
		}
	}
}
