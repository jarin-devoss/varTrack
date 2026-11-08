package healer

import (
	"encoding/json"
	"testing"
)

func TestAVerdictReadsAsItsWordsInJSON(t *testing.T) {
	out, err := json.Marshal(map[string]GitState{"a": GitStateDrifted, "b": GitStateInSync, "c": GitStateSyncing, "d": GitStateUnknown})
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != `{"a":"drifted","b":"in_sync","c":"syncing","d":"unknown"}` {
		t.Errorf("verdicts = %s", out)
	}
}
