package watcher

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"go.mongodb.org/mongo-driver/bson"

	"watcher-service/internal/healer"
)

// checkingWatcher is a fakeWatcher that also answers the orchestrator's
// "does it match Git?" question.
type checkingWatcher struct {
	fakeWatcher
	state    healer.GitState
	err      error
	checkCnt atomic.Int32
}

func (c *checkingWatcher) CheckGit(context.Context) (healer.GitState, string, error) {
	c.checkCnt.Add(1)
	return c.state, "", c.err
}

func runChecked(t *testing.T, cw *checkingWatcher) (*countingMetrics, *StateStore) {
	t.Helper()
	store, err := NewStateStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewStateStore: %v", err)
	}
	m := &countingMetrics{}
	mgr := NewManager(store, 20*time.Millisecond, m)
	mgr.Add(cw)
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	_ = mgr.Run(ctx)
	return m, store
}

func TestManager_changeThatMatchesGitIsNotHealed(t *testing.T) {
	// A push synced by varTrack: the fingerprint moves, but the orchestrator
	// says the datasource matches Git.
	cw := &checkingWatcher{fakeWatcher: fakeWatcher{name: "test", snapshots: []string{"before", "after"}}, state: healer.GitStateInSync}
	m, store := runChecked(t, cw)

	if cw.restoreCnt.Load() != 0 {
		t.Errorf("Restore called %d times, want 0", cw.restoreCnt.Load())
	}
	if m.driftCount.Load() != 0 {
		t.Errorf("drift counted %d times, want 0", m.driftCount.Load())
	}
	if got := store.Load("test"); got != "after" {
		t.Errorf("baseline = %q, want the new fingerprint", got)
	}
	// Once at startup (no saved baseline) and once for the change; the new baseline is stable.
	if cw.checkCnt.Load() != 2 {
		t.Errorf("asked the orchestrator %d times, want twice", cw.checkCnt.Load())
	}
}

func TestManager_healsAtStartupWhenGitAlreadyDiffers(t *testing.T) {
	// Drift from before the watcher ever ran: the fingerprint never moves,
	// so only the startup check can find it.
	cw := &checkingWatcher{fakeWatcher: fakeWatcher{name: "test", snapshots: []string{"drifted"}}, state: healer.GitStateDrifted}
	m, _ := runChecked(t, cw)

	if cw.restoreCnt.Load() != 1 {
		t.Errorf("Restore called %d times, want once at startup", cw.restoreCnt.Load())
	}
	if m.driftCount.Load() != 1 {
		t.Errorf("drift counted %d times, want 1", m.driftCount.Load())
	}
}

func TestManager_startupCheckOnlyHealsDefiniteDrift(t *testing.T) {
	for _, tc := range []struct {
		name  string
		state healer.GitState
		err   error
	}{
		{"unknown", healer.GitStateUnknown, nil},
		{"syncing", healer.GitStateSyncing, nil},
		{"orchestrator down", healer.GitStateUnknown, errors.New("unavailable")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cw := &checkingWatcher{fakeWatcher: fakeWatcher{name: "test", snapshots: []string{"same"}}, state: tc.state, err: tc.err}
			runChecked(t, cw)
			if cw.restoreCnt.Load() != 0 {
				t.Errorf("Restore called %d times with no change seen, want 0", cw.restoreCnt.Load())
			}
		})
	}
}

func TestManager_savedBaselineSkipsTheStartupCheck(t *testing.T) {
	store, err := NewStateStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewStateStore: %v", err)
	}
	if err := store.Save("test", "same"); err != nil {
		t.Fatalf("Save: %v", err)
	}
	cw := &checkingWatcher{fakeWatcher: fakeWatcher{name: "test", snapshots: []string{"same"}}, state: healer.GitStateDrifted}
	mgr := NewManager(store, 20*time.Millisecond, &countingMetrics{})
	mgr.Add(cw)
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_ = mgr.Run(ctx)

	// A saved baseline was compared with Git when it was saved; a change since shows up as a new fingerprint.
	if cw.checkCnt.Load() != 0 {
		t.Errorf("asked the orchestrator %d times, want 0", cw.checkCnt.Load())
	}
}

func TestManager_changeThatDiffersFromGitIsHealed(t *testing.T) {
	cw := &checkingWatcher{fakeWatcher: fakeWatcher{name: "test", snapshots: []string{"before", "tampered"}}, state: healer.GitStateDrifted}
	m, _ := runChecked(t, cw)

	if cw.restoreCnt.Load() == 0 {
		t.Error("expected Restore for a change that differs from Git")
	}
	if m.driftCount.Load() == 0 {
		t.Error("expected drift to be counted")
	}
}

func TestManager_waitsWhileASyncIsRunning(t *testing.T) {
	cw := &checkingWatcher{fakeWatcher: fakeWatcher{name: "test", snapshots: []string{"before", "writing"}}, state: healer.GitStateSyncing}
	_, store := runChecked(t, cw)

	if cw.restoreCnt.Load() != 0 {
		t.Errorf("Restore called %d times while a sync was running, want 0", cw.restoreCnt.Load())
	}
	if got := store.Load("test"); got != "before" {
		t.Errorf("baseline = %q, want it kept until the sync finishes", got)
	}
	if cw.checkCnt.Load() < 2 {
		t.Errorf("asked the orchestrator %d times, want it asked again on later polls", cw.checkCnt.Load())
	}
}

func TestManager_healsWhenGitCannotBeChecked(t *testing.T) {
	// Older orchestrator without CheckDatasource, or unreachable: keep the old behaviour.
	cw := &checkingWatcher{fakeWatcher: fakeWatcher{name: "test", snapshots: []string{"before", "after"}}, err: errors.New("unimplemented")}
	runChecked(t, cw)

	if cw.restoreCnt.Load() == 0 {
		t.Error("expected Restore when the check fails")
	}
}

func TestExtractRecords_keepsEnvironmentsApart(t *testing.T) {
	doc := func(env, value string) bson.M {
		return bson.M{VTTenantField: "acme", VTDatasourceField: "mongo", VTEnvField: env, "key": "api.rate_limit", "value": value}
	}
	records := map[string]string{}
	extractRecords(doc("production", "6000"), records)
	extractRecords(doc("staging", "1200"), records)

	if len(records) != 2 {
		t.Fatalf("got %d records %v, want one per environment", len(records), records)
	}
	if records["acme/mongo/production/api.rate_limit"] != "6000" || records["acme/mongo/staging/api.rate_limit"] != "1200" {
		t.Errorf("records = %v", records)
	}

	// Editing one environment must change the fingerprint, whichever document comes last.
	tampered := map[string]string{}
	extractRecords(doc("production", "1"), tampered)
	extractRecords(doc("staging", "1200"), tampered)
	if FingerprintRecords(records) == FingerprintRecords(tampered) {
		t.Error("a change to production did not change the fingerprint")
	}
}

func TestExtractRecords_readsFlatAndNestedData(t *testing.T) {
	scope := bson.M{VTTenantField: "acme", VTDatasourceField: "mongo", VTEnvField: "production"}
	withData := func(data any) bson.M {
		doc := bson.M{"data": data}
		for k, v := range scope {
			doc[k] = v
		}
		return doc
	}
	cases := map[string]any{
		// STRATEGY_FILE of a .properties file: flat keys.
		"flat": bson.M{"api.rate_limit": "6000"},
		// STRATEGY_DOCUMENT, decoded as bson.M or as bson.D.
		"nested bson.M": bson.M{"api": bson.M{"rate_limit": int32(6000)}},
		"nested bson.D": bson.D{{Key: "api", Value: bson.D{{Key: "rate_limit", Value: int32(6000)}}}},
	}
	for name, data := range cases {
		records := map[string]string{}
		extractRecords(withData(data), records)
		if got := records["acme/mongo/production/api.rate_limit"]; got != "6000" {
			t.Errorf("%s: records = %v", name, records)
		}
	}

	// An edit inside a nested document must change the fingerprint.
	before, after := map[string]string{}, map[string]string{}
	extractRecords(withData(bson.D{{Key: "api", Value: bson.D{{Key: "rate_limit", Value: int32(6000)}}}}), before)
	extractRecords(withData(bson.D{{Key: "api", Value: bson.D{{Key: "rate_limit", Value: int32(1)}}}}), after)
	if FingerprintRecords(before) == FingerprintRecords(after) {
		t.Error("a change inside data did not change the fingerprint")
	}
}
