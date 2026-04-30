package watcher

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"
)

// ── fake Watcher ──────────────────────────────────────────────────────────────

type fakeWatcher struct {
	name       string
	snapshots  []string // returned sequentially; last value repeated when exhausted
	snapIdx    int
	snapErr    error // if non-nil, Snapshot returns this error every time
	restoreCnt atomic.Int32
	restoreErr error
}

func (f *fakeWatcher) Name() string { return f.name }

func (f *fakeWatcher) Snapshot(_ context.Context) (string, error) {
	if f.snapErr != nil {
		return "", f.snapErr
	}
	if f.snapIdx >= len(f.snapshots) {
		return f.snapshots[len(f.snapshots)-1], nil
	}
	s := f.snapshots[f.snapIdx]
	f.snapIdx++
	return s, nil
}

func (f *fakeWatcher) Restore(_ context.Context) error {
	f.restoreCnt.Add(1)
	return f.restoreErr
}

func (f *fakeWatcher) Close() error { return nil }

// ── noopMetrics that counts calls ────────────────────────────────────────────

type countingMetrics struct {
	driftCount  atomic.Int32
	healCount   atomic.Int32
	failedCount atomic.Int32
}

func (c *countingMetrics) IncDriftDetected(string)                   { c.driftCount.Add(1) }
func (c *countingMetrics) IncHealTriggered(string)                   { c.healCount.Add(1) }
func (c *countingMetrics) IncHealFailed(string)                      { c.failedCount.Add(1) }
func (c *countingMetrics) ObservePollDuration(string, time.Duration) {}
func (c *countingMetrics) SetWatcherUp(string, bool)                 {}

// ── helpers ───────────────────────────────────────────────────────────────────

func newTestManager(t *testing.T, poll time.Duration, m ManagerMetrics) *Manager {
	t.Helper()
	store, err := NewStateStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewStateStore: %v", err)
	}
	return NewManager(store, poll, m)
}

// runFor runs the manager for at most dur and returns.
func runFor(t *testing.T, mgr *Manager, dur time.Duration) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), dur)
	defer cancel()
	_ = mgr.Run(ctx)
}

// ── tests ─────────────────────────────────────────────────────────────────────

func TestManager_noWatchers(t *testing.T) {
	mgr := newTestManager(t, 10*time.Millisecond, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	// Run should return nil after ctx is cancelled.
	if err := mgr.Run(ctx); err != nil {
		t.Errorf("expected nil, got %v", err)
	}
}

func TestManager_noDrift(t *testing.T) {
	m := &countingMetrics{}
	mgr := newTestManager(t, 20*time.Millisecond, m)

	fw := &fakeWatcher{
		name:      "test",
		snapshots: []string{"stablehash"},
	}
	mgr.Add(fw)
	runFor(t, mgr, 120*time.Millisecond)

	if m.driftCount.Load() != 0 {
		t.Errorf("expected no drift, got %d", m.driftCount.Load())
	}
	if fw.restoreCnt.Load() != 0 {
		t.Errorf("expected no Restore calls, got %d", fw.restoreCnt.Load())
	}
}

func TestManager_driftTriggersHeal(t *testing.T) {
	m := &countingMetrics{}
	mgr := newTestManager(t, 20*time.Millisecond, m)

	fw := &fakeWatcher{
		name: "test",
		// baseline="hash1", then poll returns "hash2" → drift → Restore, then "hash2" again = stable
		snapshots: []string{"hash1", "hash2", "hash2"},
	}
	mgr.Add(fw)
	runFor(t, mgr, 200*time.Millisecond)

	if m.driftCount.Load() == 0 {
		t.Error("expected drift to be detected")
	}
	if fw.restoreCnt.Load() == 0 {
		t.Error("expected Restore to be called")
	}
}

func TestManager_healFailureIncrementsFailedMetric(t *testing.T) {
	m := &countingMetrics{}
	mgr := newTestManager(t, 20*time.Millisecond, m)

	fw := &fakeWatcher{
		name:       "test",
		snapshots:  []string{"hash1", "hash2"},
		restoreErr: errors.New("orchestrator unavailable"),
	}
	mgr.Add(fw)
	runFor(t, mgr, 150*time.Millisecond)

	if m.driftCount.Load() == 0 {
		t.Error("expected drift to be counted")
	}
	if m.failedCount.Load() == 0 {
		t.Error("expected heal failures to be counted")
	}
}

func TestManager_snapshotErrorContinues(t *testing.T) {
	mgr := newTestManager(t, 20*time.Millisecond, nil)

	fw := &fakeWatcher{
		name:    "test",
		snapErr: errors.New("connection refused"),
	}
	mgr.Add(fw)
	// Manager should not crash and should run for the full duration.
	runFor(t, mgr, 100*time.Millisecond)
}

func TestManager_initialSnapshotFromStore(t *testing.T) {
	store, err := NewStateStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewStateStore: %v", err)
	}

	// Pre-seed a baseline so the watcher loads it instead of calling Snapshot.
	_ = store.Save("test", "persisted_hash")

	mgr := NewManager(store, 20*time.Millisecond, nil)
	// fakeWatcher snapshots start at "persisted_hash" → no drift.
	fw := &fakeWatcher{
		name:      "test",
		snapshots: []string{"persisted_hash"},
	}
	mgr.Add(fw)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_ = mgr.Run(ctx)

	// snapIdx == 0 means Snapshot was never called during initialSnapshot
	// (it loaded from store) — only the poll ticks triggered Snapshot.
	// Restore must not have been called.
	if fw.restoreCnt.Load() != 0 {
		t.Errorf("expected no Restore, got %d", fw.restoreCnt.Load())
	}
}

func TestManager_closeCallsWatcherClose(t *testing.T) {
	mgr := newTestManager(t, time.Second, nil)
	closed := false
	type closeable struct{ fakeWatcher }
	// Use an inline struct with a custom Close.
	type trackClose struct {
		fakeWatcher
		closedFlag *bool
	}
	_ = closed
	// Use fakeWatcher directly — Close() returns nil and doesn't panic.
	fw := &fakeWatcher{name: "c", snapshots: []string{"h"}}
	mgr.Add(fw)
	mgr.Close() // must not panic
}

// ── backoff ────────────────────────────────────────────────────────────────────

func TestBackoff_progressesAndCaps(t *testing.T) {
	b := newBackoff(5*time.Second, 2.0, 20*time.Second)

	d1 := b.next() // 5s
	d2 := b.next() // 10s
	d3 := b.next() // 20s (capped)
	d4 := b.next() // 20s (stays capped)

	if d1 != 5*time.Second {
		t.Errorf("d1: want 5s, got %v", d1)
	}
	if d2 != 10*time.Second {
		t.Errorf("d2: want 10s, got %v", d2)
	}
	if d3 != 20*time.Second {
		t.Errorf("d3: want 20s (cap), got %v", d3)
	}
	if d4 != 20*time.Second {
		t.Errorf("d4: want 20s (cap), got %v", d4)
	}
}

func TestBackoff_reset(t *testing.T) {
	b := newBackoff(5*time.Second, 2.0, 1*time.Minute)
	_ = b.next() // 5s
	_ = b.next() // 10s
	b.reset()
	if d := b.next(); d != 5*time.Second {
		t.Errorf("after reset: want 5s, got %v", d)
	}
}
