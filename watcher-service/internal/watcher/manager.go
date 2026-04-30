// manager.go — WatcherManager coordinates all watcher goroutines.
//
// Runs one poll-and-heal loop per (datasource, self_heal=true) rule.
// Each loop is independent; a failure in one does not affect others.
package watcher

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"golang.org/x/sync/errgroup"

	mon "watcher-service/internal/monitoring"
)

// Manager owns and runs all active Watcher instances.
type Manager struct {
	watchers     []Watcher
	store        StateBackend
	pollInterval time.Duration
	metrics      ManagerMetrics
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

// NewManager creates a Manager.
// store may be a *StateStore (disk) or *RedisStateStore (Redis).
// metrics may be nil — a no-op implementation will be used.
func NewManager(
	store StateBackend,
	pollInterval time.Duration,
	metrics ManagerMetrics,
) *Manager {
	m := &Manager{store: store, pollInterval: pollInterval}
	if metrics != nil {
		m.metrics = metrics
	} else {
		m.metrics = noopMetrics{}
	}
	return m
}

// Add registers a watcher.  Must be called before Run().
func (m *Manager) Add(w Watcher) {
	m.watchers = append(m.watchers, w)
}

// ActiveCount returns the number of registered watchers.
// Used by the admin readiness probe.
func (m *Manager) ActiveCount() int {
	return len(m.watchers)
}

// Run starts all watchers in separate goroutines and blocks until ctx is
// cancelled or all watchers exit.
//
// Each watcher:
//   1. Takes an initial snapshot to establish the baseline fingerprint.
//   2. Enters the poll loop: every pollInterval, re-snapshot and compare.
//   3. On drift: call w.Restore() with exponential backoff.
//   4. After a successful restore: re-snapshot and update baseline.
func (m *Manager) Run(ctx context.Context) error {
	if len(m.watchers) == 0 {
		slog.Info("watcher manager: no watchers configured, nothing to do")
		<-ctx.Done()
		return nil
	}

	g, ctx := errgroup.WithContext(ctx)
	for _, w := range m.watchers {
		w := w // capture
		g.Go(func() error {
			return m.runOne(ctx, w)
		})
	}
	return g.Wait()
}

// Close shuts down all watchers.
func (m *Manager) Close() {
	for _, w := range m.watchers {
		if err := w.Close(); err != nil {
			slog.Warn("watcher: close error", "watcher", w.Name(), "error", err)
		}
	}
}

// ─── internal ─────────────────────────────────────────────────────────────────

func (m *Manager) runOne(ctx context.Context, w Watcher) error {
	name := w.Name()
	slog.Info("watcher: starting", "watcher", name, "poll_interval", m.pollInterval)
	m.metrics.SetWatcherUp(name, true)
	defer m.metrics.SetWatcherUp(name, false)

	// 1. Establish baseline fingerprint.
	baseline, err := m.initialSnapshot(ctx, w)
	if err != nil {
		slog.Error("watcher: initial snapshot failed — watcher will not run",
			"watcher", name, "error", err)
		return nil // do not crash manager; one bad watcher shouldn't stop others
	}

	ticker := time.NewTicker(m.pollInterval)
	defer ticker.Stop()

	backoff := newBackoff(5*time.Second, 2.0, 5*time.Minute)

	for {
		select {
		case <-ctx.Done():
			slog.Info("watcher: stopping", "watcher", name)
			return nil

		case <-ticker.C:
			t0 := time.Now()
			pollCtx, pollSpan := mon.StartPollSpan(ctx, name, name)
			current, err := w.Snapshot(pollCtx)
			m.metrics.ObservePollDuration(name, time.Since(t0))
			pollSpan.End(err)
			if err != nil {
				slog.Warn("watcher: snapshot error", "watcher", name, "error", err)
				continue
			}

			if current == baseline {
				hashPrefix := current
				if len(hashPrefix) > 8 {
					hashPrefix = hashPrefix[:8]
				}
				slog.Debug("watcher: no drift", "watcher", name, "hash", hashPrefix)
				backoff.reset()
				continue
			}

			// Drift detected.
			m.metrics.IncDriftDetected(name)
			LogDrift(DriftRecord{
				WatcherName: name,
				Datasource:  name,
				OldHash:     baseline,
				NewHash:     current,
				DetectedAt:  time.Now().UTC(),
			})

			// Trigger heal with backoff.
			if healErr := m.heal(ctx, w, backoff); healErr != nil {
				m.metrics.IncHealFailed(name)
				slog.Error("watcher: heal failed", "watcher", name, "error", healErr)
				// Keep old baseline — will re-detect on next tick.
				continue
			}

			// Heal succeeded — update baseline.
			m.metrics.IncHealTriggered(name)
			newBaseline, _ := w.Snapshot(ctx)
			if newBaseline != "" {
				baseline = newBaseline
				if err := m.store.Save(name, baseline); err != nil {
					slog.Warn("watcher: failed to persist baseline",
						"watcher", name, "error", err)
				}
			}
			backoff.reset()
		}
	}
}

// initialSnapshot loads the persisted fingerprint (survives restarts) or,
// if none exists, takes a fresh snapshot and saves it as the baseline.
func (m *Manager) initialSnapshot(ctx context.Context, w Watcher) (string, error) {
	if saved := m.store.Load(w.Name()); saved != "" {
		prefix := saved
		if len(prefix) > 8 {
			prefix = prefix[:8]
		}
		slog.Info("watcher: loaded persisted baseline",
			"watcher", w.Name(), "hash", prefix)
		return saved, nil
	}

	slog.Info("watcher: no persisted baseline — taking initial snapshot",
		"watcher", w.Name())
	fp, err := w.Snapshot(ctx)
	if err != nil {
		return "", err
	}
	if err := m.store.Save(w.Name(), fp); err != nil {
		slog.Warn("watcher: failed to persist initial baseline",
			"watcher", w.Name(), "error", err)
	}
	prefix := fp
	if len(prefix) > 8 {
		prefix = prefix[:8]
	}
	slog.Info("watcher: baseline established", "watcher", w.Name(), "hash", prefix)
	return fp, nil
}

// heal calls w.Restore() with exponential backoff until it succeeds, ctx is
// cancelled, or maxHealAttempts is exhausted.
const maxHealAttempts = 5

func (m *Manager) heal(ctx context.Context, w Watcher, b *backoffState) error {
	for attempt := 1; ; attempt++ {
		healCtx, healSpan := mon.StartHealSpan(ctx, w.Name(), w.Name())
		err := w.Restore(healCtx)
		healSpan.End(err)
		if err == nil {
			return nil
		}
		if attempt >= maxHealAttempts {
			return fmt.Errorf("heal: gave up after %d attempts, last error: %w", maxHealAttempts, err)
		}
		wait := b.next()
		slog.Warn("watcher: heal attempt failed, retrying",
			"watcher", w.Name(), "error", err, "retry_in", wait, "attempt", attempt, "max", maxHealAttempts)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(wait):
		}
	}
}

// ─── exponential backoff ──────────────────────────────────────────────────────

type backoffState struct {
	current    time.Duration
	initial    time.Duration
	max        time.Duration
	multiplier float64
}

func newBackoff(initial time.Duration, multiplier float64, max time.Duration) *backoffState {
	return &backoffState{current: initial, initial: initial, max: max, multiplier: multiplier}
}

func (b *backoffState) next() time.Duration {
	d := b.current
	next := time.Duration(float64(b.current) * b.multiplier)
	if next > b.max {
		next = b.max
	}
	b.current = next
	return d
}

func (b *backoffState) reset() { b.current = b.initial }
