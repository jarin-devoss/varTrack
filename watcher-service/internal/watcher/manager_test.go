package watcher

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"watcher-service/internal/healer"
)

// ── fake orchestrator ─────────────────────────────────────────────────────────

type fakeOrchestrator struct {
	mu       sync.Mutex
	states   []healer.GitState // answered in turn; the last one repeats
	checkErr error
	healErr  error
	checks   atomic.Int32
	heals    atomic.Int32
	reasons  []string

	// listings are answered to ListTargets in turn; the last one repeats. The
	// first failBefore asks fail, and so does every ask from failFrom on (when
	// it is set).
	listings   [][]healer.WatchTarget
	failBefore int
	failFrom   int
	lists      atomic.Int32

	requests []healer.HealRequest // every Check and Heal, by datasource+tenant
	checked  map[string]int       // Check calls by "tenant|platform/datasource"
}

var oneRule = []healer.WatchTarget{{Platform: "gitea", Datasource: "redis", Tenant: "acme"}}

func (f *fakeOrchestrator) ListTargets(context.Context) ([]healer.WatchTarget, error) {
	n := int(f.lists.Add(1)) - 1
	f.mu.Lock()
	defer f.mu.Unlock()
	if n < f.failBefore || (f.failFrom > 0 && n >= f.failFrom) {
		return nil, errors.New("orchestrator unavailable")
	}
	if len(f.listings) == 0 {
		return oneRule, nil
	}
	return f.listings[min(n, len(f.listings)-1)], nil
}

func (f *fakeOrchestrator) Check(_ context.Context, req healer.HealRequest) (healer.GitState, string, error) {
	n := int(f.checks.Add(1)) - 1
	f.mu.Lock()
	if f.checked == nil {
		f.checked = map[string]int{}
	}
	f.checked[req.Tenant+"|"+req.Platform+"/"+req.Datasource]++
	f.requests = append(f.requests, req)
	f.mu.Unlock()
	if f.checkErr != nil {
		return healer.GitStateUnknown, "", f.checkErr
	}
	if len(f.states) == 0 {
		return healer.GitStateInSync, "", nil
	}
	if n >= len(f.states) {
		n = len(f.states) - 1
	}
	return f.states[n], "redis/production differs from Git", nil
}

func (f *fakeOrchestrator) Heal(_ context.Context, req healer.HealRequest) error {
	f.heals.Add(1)
	f.mu.Lock()
	f.reasons = append(f.reasons, req.Reason)
	f.mu.Unlock()
	return f.healErr
}

func (f *fakeOrchestrator) checksOf(key string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.checked[key]
}

// ── counting metrics ──────────────────────────────────────────────────────────

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

// newManager polls every 10ms and retries the first ask after 1ms.
func newManager(orch *fakeOrchestrator, m ManagerMetrics) *Manager {
	mgr := NewManager(orch, 10*time.Millisecond, m)
	mgr.firstRetry = time.Millisecond
	return mgr
}

// run watches what the orchestrator lists for 115ms (about 12 polls) with grace.
func run(t *testing.T, orch *fakeOrchestrator, grace time.Duration) *countingMetrics {
	t.Helper()
	m := &countingMetrics{}
	mgr := newManager(orch, m)
	mgr.healGrace = grace
	ctx, cancel := context.WithTimeout(context.Background(), 115*time.Millisecond)
	defer cancel()
	if err := mgr.Run(ctx); err != nil {
		t.Fatalf("Run: %v", err)
	}
	return m
}

// eventually waits up to a second for cond.
func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// ── checking and healing ──────────────────────────────────────────────────────

func TestInSyncIsNeverHealed(t *testing.T) {
	orch := &fakeOrchestrator{states: []healer.GitState{healer.GitStateInSync}}
	m := run(t, orch, 0)
	if orch.checks.Load() < 5 {
		t.Errorf("checked %d times, want one per poll", orch.checks.Load())
	}
	if orch.heals.Load() != 0 || m.driftCount.Load() != 0 {
		t.Errorf("heals=%d drift=%d, want none", orch.heals.Load(), m.driftCount.Load())
	}
}

func TestDriftIsHealedOnceWhileTheHealIsUnderway(t *testing.T) {
	orch := &fakeOrchestrator{states: []healer.GitState{healer.GitStateDrifted}}
	m := run(t, orch, time.Hour)
	if orch.heals.Load() != 1 {
		t.Errorf("healed %d times, want once: the drift seen after is the heal's to fix", orch.heals.Load())
	}
	if m.healCount.Load() != 1 || m.driftCount.Load() != 1 {
		t.Errorf("metrics heal=%d drift=%d, want 1/1", m.healCount.Load(), m.driftCount.Load())
	}
	if orch.reasons[0] != "self-heal: redis/production differs from Git" {
		t.Errorf("reason = %q", orch.reasons[0])
	}
}

func TestDriftAfterTheGraceIsHealedAgain(t *testing.T) {
	orch := &fakeOrchestrator{states: []healer.GitState{healer.GitStateDrifted}}
	run(t, orch, 0)
	if orch.heals.Load() < 5 {
		t.Errorf("healed %d times, want every poll once the grace is over", orch.heals.Load())
	}
}

func TestBackInSyncEndsTheGrace(t *testing.T) {
	orch := &fakeOrchestrator{states: []healer.GitState{
		healer.GitStateDrifted, healer.GitStateSyncing, healer.GitStateInSync, healer.GitStateDrifted,
	}}
	run(t, orch, time.Hour)
	if orch.heals.Load() != 2 {
		t.Errorf("healed %d times, want twice: new drift after the heal finished", orch.heals.Load())
	}
}

func TestNoEvidenceNoHeal(t *testing.T) {
	for name, orch := range map[string]*fakeOrchestrator{
		"syncing": {states: []healer.GitState{healer.GitStateSyncing}},
		"unknown": {states: []healer.GitState{healer.GitStateUnknown}},
		"error":   {checkErr: errors.New("orchestrator down")},
	} {
		t.Run(name, func(t *testing.T) {
			run(t, orch, 0)
			if orch.heals.Load() != 0 {
				t.Errorf("healed %d times, want none", orch.heals.Load())
			}
		})
	}
}

func TestAFailedHealIsRetriedNextPoll(t *testing.T) {
	orch := &fakeOrchestrator{states: []healer.GitState{healer.GitStateDrifted}, healErr: errors.New("unavailable")}
	m := run(t, orch, time.Hour)
	if orch.heals.Load() < 5 || m.failedCount.Load() != orch.heals.Load() {
		t.Errorf("heals=%d failed=%d, want a retry every poll", orch.heals.Load(), m.failedCount.Load())
	}
}

func TestHealGraceIsTwoPollsAtLeast30s(t *testing.T) {
	if g := NewManager(&fakeOrchestrator{}, time.Minute, nil).healGrace; g != 2*time.Minute {
		t.Errorf("heal grace = %v, want two polls", g)
	}
	if NewManager(&fakeOrchestrator{}, time.Second, nil).healGrace != minHealGrace {
		t.Error("a short poll should still give a heal 30s to start")
	}
}

// ── following the orchestrator's list ─────────────────────────────────────────

func TestTheTargetIsWhatTheOrchestratorListed(t *testing.T) {
	tg := NewTarget(healer.WatchTarget{Platform: "gitea", Datasource: "mongo-primary", Tenant: "acme"})
	if tg.Name != "gitea/mongo-primary" || tg.Tenant != "acme" {
		t.Errorf("target = %+v", tg)
	}
	if r := tg.Request; r.Datasource != "mongo-primary" || r.Platform != "gitea" || r.Tenant != "acme" {
		t.Errorf("request = %+v, want the datasource, platform and tenant it was listed with", r)
	}
}

func TestTheSameRuleOfTwoTenantsIsTwoTargets(t *testing.T) {
	orch := &fakeOrchestrator{listings: [][]healer.WatchTarget{{
		{Platform: "gitea", Datasource: "mongo", Tenant: "acme"},
		{Platform: "gitea", Datasource: "mongo", Tenant: "globex"},
		{Platform: "gitea", Datasource: "mongo", Tenant: "globex"}, // listed twice: one target
	}}}
	run(t, orch, 0)

	if orch.checksOf("acme|gitea/mongo") < 3 || orch.checksOf("globex|gitea/mongo") < 3 {
		t.Errorf("checks = %v, want each tenant's rule checked every poll", orch.checked)
	}
}

func TestRuleChangesReachTheWatcherWithoutARestart(t *testing.T) {
	a := healer.WatchTarget{Platform: "gitea", Datasource: "redis", Tenant: "acme"}
	b := healer.WatchTarget{Platform: "gitea", Datasource: "mongo", Tenant: "acme"}
	// the first ask lists a; the second a and b; every later one only b
	orch := &fakeOrchestrator{listings: [][]healer.WatchTarget{{a}, {a, b}, {b}}}
	mgr := newManager(orch, nil)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- mgr.Run(ctx) }()

	eventually(t, "b to be watched", func() bool { return orch.checksOf("acme|gitea/mongo") > 0 })
	eventually(t, "a to be dropped", func() bool { return mgr.ActiveCount() == 1 })
	checksOfA := orch.checksOf("acme|gitea/redis")
	time.Sleep(60 * time.Millisecond) // several polls
	if got := orch.checksOf("acme|gitea/redis"); got > checksOfA+1 {
		t.Errorf("a was checked %d more times after the orchestrator stopped listing it", got-checksOfA)
	}
	if orch.checksOf("acme|gitea/mongo") < 3 {
		t.Errorf("b is checked %d times, want every poll", orch.checksOf("acme|gitea/mongo"))
	}

	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestAnEmptyListStopsEveryLoop(t *testing.T) {
	orch := &fakeOrchestrator{listings: [][]healer.WatchTarget{oneRule, nil}}
	mgr := newManager(orch, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = mgr.Run(ctx) }()

	eventually(t, "the rule to be watched", func() bool { return orch.checks.Load() > 0 })
	eventually(t, "the loop to stop", func() bool { return mgr.ActiveCount() == 0 })
}

func TestACallThatFailsKeepsTheLoopsItHas(t *testing.T) {
	// The first ask answers; every later one fails (the orchestrator restarts).
	orch := &fakeOrchestrator{failFrom: 1}
	mgr := newManager(orch, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 115*time.Millisecond)
	defer cancel()

	if err := mgr.Run(ctx); err != nil {
		t.Fatal(err)
	}

	if orch.lists.Load() < 4 {
		t.Fatalf("asked %d times, want one per poll", orch.lists.Load())
	}
	if orch.checks.Load() < 8 {
		t.Errorf("checked %d times: the rule stopped being watched while the list could not be had", orch.checks.Load())
	}
}

func TestBeforeTheFirstAnswerItAsksAgain(t *testing.T) {
	orch := &fakeOrchestrator{failBefore: 3} // not up yet
	mgr := newManager(orch, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 115*time.Millisecond)
	defer cancel()

	if err := mgr.Run(ctx); err != nil {
		t.Fatal(err)
	}

	if orch.lists.Load() < 4 || orch.checks.Load() == 0 {
		t.Errorf("asked %d times, checked %d: want it to retry until the orchestrator answers, then watch",
			orch.lists.Load(), orch.checks.Load())
	}
}

func TestStoppingBeforeAnyAnswerReturnsAtOnce(t *testing.T) {
	orch := &fakeOrchestrator{failBefore: 1 << 30} // never answers
	mgr := newManager(orch, nil)
	mgr.firstRetry = time.Hour
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- mgr.Run(ctx) }()
	eventually(t, "the first ask", func() bool { return orch.lists.Load() == 1 })

	cancel()

	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("Run did not return after ctx was cancelled while waiting to ask again")
	}
}

func TestALeaderThatLosesTheLockAndWinsItAgainWatchesAgain(t *testing.T) {
	orch := &fakeOrchestrator{}
	mgr := newManager(orch, nil)

	for round := 1; round <= 2; round++ {
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan error, 1)
		before := orch.checks.Load()
		go func() { done <- mgr.Run(ctx) }()
		eventually(t, "the rule to be checked", func() bool { return orch.checks.Load() > before })
		cancel()
		if err := <-done; err != nil {
			t.Fatal(err)
		}
		if n := mgr.ActiveCount(); n != 0 {
			t.Fatalf("round %d: %d loops still running after Run returned", round, n)
		}
	}
}
