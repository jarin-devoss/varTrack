// manager.go — runs one check-and-heal loop per self_heal rule the
// orchestrator lists, and follows that list as it changes.
//
// Each loop is independent; a failure in one does not affect others.
package watcher

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"watcher-service/internal/healer"
	mon "watcher-service/internal/monitoring"
)

// minHealGrace is the least time a requested heal gets to start before the
// same drift asks for another one.
const minHealGrace = 30 * time.Second

// firstRetry is how long the manager waits before asking again when the
// orchestrator has not answered yet; the wait doubles up to one poll interval.
const firstRetry = time.Second

// Manager asks the orchestrator which rules to watch, and about each of them,
// every poll interval.
type Manager struct {
	orch         Orchestrator
	pollInterval time.Duration
	metrics      ManagerMetrics

	// healGrace: after a heal is requested, a "drifted" answer within this
	// long is the drift that heal is about to fix — the sync it queued may
	// not have started yet — not a reason to queue another.
	healGrace time.Duration

	// firstRetry is the first wait between asks while no answer has come.
	firstRetry time.Duration

	mu    sync.Mutex
	loops map[string]*loop // the running loops, by Target.Key
	wg    sync.WaitGroup
}

// loop is the check-and-heal loop of one target.
type loop struct {
	target Target
	cancel context.CancelFunc
}

// ManagerMetrics is satisfied by *monitoring.WatcherMetrics.
// Defined as an interface here to avoid a circular import.
type ManagerMetrics interface {
	IncDriftDetected(datasource string)
	IncHealTriggered(datasource string)
	IncHealFailed(datasource string)
	ObservePollDuration(datasource string, d time.Duration)
	SetWatcherUp(datasource string, up bool)
}

// noopMetrics satisfies ManagerMetrics when no metrics backend is wired.
type noopMetrics struct{}

func (noopMetrics) IncDriftDetected(string)                   {}
func (noopMetrics) IncHealTriggered(string)                   {}
func (noopMetrics) IncHealFailed(string)                      {}
func (noopMetrics) ObservePollDuration(string, time.Duration) {}
func (noopMetrics) SetWatcherUp(string, bool)                 {}

// NewManager creates a Manager that asks orch every pollInterval.
// metrics may be nil — a no-op implementation will be used.
func NewManager(orch Orchestrator, pollInterval time.Duration, metrics ManagerMetrics) *Manager {
	m := &Manager{
		orch:         orch,
		pollInterval: pollInterval,
		metrics:      metrics,
		healGrace:    max(2*pollInterval, minHealGrace),
		firstRetry:   firstRetry,
		loops:        map[string]*loop{},
	}
	if metrics == nil {
		m.metrics = noopMetrics{}
	}
	return m
}

// ActiveCount returns the number of targets being watched.
// Used by the admin readiness probe.
func (m *Manager) ActiveCount() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.loops)
}

// Run asks the orchestrator which rules to watch, starts a loop for each, and
// asks again every poll interval, starting loops for new rules and stopping
// the ones whose rule is gone, until ctx is cancelled. It can be called
// again after it returns (a leader that lost and regained the lock).
func (m *Manager) Run(ctx context.Context) error {
	defer m.stopAll()

	if !m.discoverFirst(ctx) {
		return nil
	}
	ticker := time.NewTicker(m.pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			m.refresh(ctx)
		}
	}
}

// ─── internal ─────────────────────────────────────────────────────────────────

// discoverFirst asks until the orchestrator answers once, waiting longer each
// time (up to one poll interval). It reports false when ctx ended first.
func (m *Manager) discoverFirst(ctx context.Context) bool {
	delay := m.firstRetry
	for !m.refresh(ctx) {
		select {
		case <-ctx.Done():
			return false
		case <-time.After(delay):
		}
		delay = min(2*delay, m.pollInterval)
	}
	return true
}

// refresh asks which rules to watch and follows the answer. When the
// orchestrator cannot be asked, the loops that run keep running: it reports
// false, and the next poll asks again.
func (m *Manager) refresh(ctx context.Context) bool {
	listed, err := m.orch.ListTargets(ctx)
	if err != nil {
		slog.Warn("watcher: could not ask the orchestrator which rules to watch — keeping the ones it has",
			"watching", m.ActiveCount(), "error", err)
		return false
	}
	targets := make([]Target, len(listed))
	for i, t := range listed {
		targets[i] = NewTarget(t)
	}
	m.reconcile(ctx, targets)
	return true
}

// reconcile starts a loop for each target that has none and stops the loops
// of targets no longer listed.
func (m *Manager) reconcile(ctx context.Context, targets []Target) {
	m.mu.Lock()
	defer m.mu.Unlock()

	wanted := make(map[string]bool, len(targets))
	for _, t := range targets {
		wanted[t.Key()] = true
	}
	for key, l := range m.loops {
		if !wanted[key] {
			slog.Info("watcher: no longer a target — stopping", "watcher", l.target.Name, "tenant", l.target.Tenant)
			l.cancel()
			delete(m.loops, key)
		}
	}
	for _, t := range targets {
		if _, running := m.loops[t.Key()]; running {
			continue
		}
		loopCtx, cancel := context.WithCancel(ctx)
		m.loops[t.Key()] = &loop{target: t, cancel: cancel}
		slog.Info("watcher: registered", "watcher", t.Name, "tenant", t.Tenant)
		m.wg.Add(1)
		go func() {
			defer m.wg.Done()
			m.runOne(loopCtx, t)
		}()
	}
	if len(m.loops) == 0 {
		slog.Info("watcher manager: the orchestrator lists no self_heal rules, nothing to do")
	}
}

// stopAll stops every loop and waits for them to finish.
func (m *Manager) stopAll() {
	m.mu.Lock()
	for key, l := range m.loops {
		l.cancel()
		delete(m.loops, key)
	}
	m.mu.Unlock()
	m.wg.Wait()
}

func (m *Manager) runOne(ctx context.Context, t Target) {
	slog.Info("watcher: starting", "watcher", t.Name, "tenant", t.Tenant, "poll_interval", m.pollInterval)
	m.metrics.SetWatcherUp(t.Name, true)
	defer m.metrics.SetWatcherUp(t.Name, false)

	ticker := time.NewTicker(m.pollInterval)
	defer ticker.Stop()
	var lastHeal time.Time
	for {
		m.poll(ctx, t, &lastHeal)
		select {
		case <-ctx.Done():
			slog.Info("watcher: stopping", "watcher", t.Name, "tenant", t.Tenant)
			return
		case <-ticker.C:
		}
	}
}

// poll asks the orchestrator about t once, and heals it when it drifted.
func (m *Manager) poll(ctx context.Context, t Target, lastHeal *time.Time) {
	t0 := time.Now()
	pollCtx, pollSpan := mon.StartPollSpan(ctx, t.Name, t.Name)
	state, detail, err := m.orch.Check(pollCtx, t.Request)
	m.metrics.ObservePollDuration(t.Name, time.Since(t0))
	pollSpan.End(err)

	switch {
	case err != nil:
		slog.Warn("watcher: could not ask the orchestrator — checking again next poll",
			"watcher", t.Name, "error", err)
		return
	case state == healer.GitStateInSync:
		slog.Debug("watcher: in sync", "watcher", t.Name, "detail", detail)
		*lastHeal = time.Time{}
		return
	case state == healer.GitStateSyncing:
		slog.Info("watcher: sync in progress — checking again next poll", "watcher", t.Name, "detail", detail)
		return
	case state != healer.GitStateDrifted:
		// Never synced, or the sink could not be read: no evidence of drift.
		slog.Info("watcher: the orchestrator cannot tell — not healing", "watcher", t.Name, "detail", detail)
		return
	}

	if !lastHeal.IsZero() && time.Since(*lastHeal) < m.healGrace {
		slog.Info("watcher: still drifted — waiting for the heal already requested",
			"watcher", t.Name, "requested", lastHeal.UTC().Format(time.RFC3339))
		return
	}

	m.metrics.IncDriftDetected(t.Name)
	slog.Warn("watcher: drift detected", "watcher", t.Name, "detail", detail)

	req := t.Request
	req.Reason = "self-heal: " + detail
	healCtx, healSpan := mon.StartHealSpan(ctx, t.Name, t.Name)
	err = m.orch.Heal(healCtx, req)
	healSpan.End(err)
	if err != nil {
		m.metrics.IncHealFailed(t.Name)
		slog.Error("watcher: heal failed — trying again next poll", "watcher", t.Name, "error", err)
		return
	}
	m.metrics.IncHealTriggered(t.Name)
	*lastHeal = time.Now()
}
