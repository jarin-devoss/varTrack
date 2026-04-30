// Package monitoring provides Prometheus metrics for the watcher-service.
//
// Mirrors gateway-service/internal/monitoring/gateway_metrics.go:
//   - uses a non-global CollectorRegistry to avoid conflicts in tests
//   - typed Inc/Observe/Set methods so callers never touch raw labels
//   - GenerateLatest() exposed as []byte for the /metrics handler
package monitoring

import (
	"bytes"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/common/expfmt"
)

// WatcherMetrics holds all Prometheus instruments for the watcher-service.
type WatcherMetrics struct {
	registry *prometheus.Registry

	// Drift detection
	driftTotal   *prometheus.CounterVec
	healTotal    *prometheus.CounterVec
	healFailures *prometheus.CounterVec
	pollDuration *prometheus.HistogramVec
	watcherUp    *prometheus.GaugeVec

	// Build info
	buildInfo *prometheus.GaugeVec
}

var (
	_metrics     *WatcherMetrics
	_metricsOnce sync.Once
)

// DefaultMetrics returns the singleton WatcherMetrics, creating it on
// first call.  Subsequent calls return the same instance.
func DefaultMetrics() *WatcherMetrics {
	_metricsOnce.Do(func() {
		_metrics = newMetrics()
	})
	return _metrics
}

func newMetrics() *WatcherMetrics {
	reg := prometheus.NewRegistry()
	reg.MustRegister(
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)

	m := &WatcherMetrics{registry: reg}

	// ── Drift counter ─────────────────────────────────────────────────────────
	m.driftTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Namespace: "vartrack",
		Subsystem: "watcher",
		Name:      "drift_detected_total",
		Help:      "Total number of drift events detected per datasource.",
	}, []string{"datasource"})
	reg.MustRegister(m.driftTotal)

	// ── Heal counters ─────────────────────────────────────────────────────────
	m.healTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Namespace: "vartrack",
		Subsystem: "watcher",
		Name:      "heal_triggered_total",
		Help:      "Total number of successful heal requests sent to the orchestrator.",
	}, []string{"datasource"})
	reg.MustRegister(m.healTotal)

	m.healFailures = prometheus.NewCounterVec(prometheus.CounterOpts{
		Namespace: "vartrack",
		Subsystem: "watcher",
		Name:      "heal_failed_total",
		Help:      "Total number of heal requests that failed.",
	}, []string{"datasource"})
	reg.MustRegister(m.healFailures)

	// ── Poll duration histogram ───────────────────────────────────────────────
	m.pollDuration = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Namespace: "vartrack",
		Subsystem: "watcher",
		Name:      "poll_duration_seconds",
		Help:      "Time spent taking a datasource snapshot during each poll.",
		Buckets:   prometheus.DefBuckets,
	}, []string{"datasource"})
	reg.MustRegister(m.pollDuration)

	// ── Watcher up gauge ──────────────────────────────────────────────────────
	m.watcherUp = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Namespace: "vartrack",
		Subsystem: "watcher",
		Name:      "up",
		Help:      "1 if the watcher for this datasource is running, 0 if stopped.",
	}, []string{"datasource"})
	reg.MustRegister(m.watcherUp)

	// ── Build info ────────────────────────────────────────────────────────────
	m.buildInfo = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Namespace: "vartrack",
		Subsystem: "watcher",
		Name:      "build_info",
		Help:      "Static build metadata; value is always 1.",
	}, []string{"version", "commit", "go_version"})
	reg.MustRegister(m.buildInfo)

	return m
}

// ── Typed mutators ────────────────────────────────────────────────────────────

func (m *WatcherMetrics) IncDriftDetected(datasource string) {
	m.driftTotal.WithLabelValues(datasource).Inc()
}

func (m *WatcherMetrics) IncHealTriggered(datasource string) {
	m.healTotal.WithLabelValues(datasource).Inc()
}

func (m *WatcherMetrics) IncHealFailed(datasource string) {
	m.healFailures.WithLabelValues(datasource).Inc()
}

func (m *WatcherMetrics) ObservePollDuration(datasource string, d time.Duration) {
	m.pollDuration.WithLabelValues(datasource).Observe(d.Seconds())
}

func (m *WatcherMetrics) SetWatcherUp(datasource string, up bool) {
	v := 0.0
	if up {
		v = 1.0
	}
	m.watcherUp.WithLabelValues(datasource).Set(v)
}

func (m *WatcherMetrics) SetBuildInfo(version, commit, goVersion string) {
	m.buildInfo.WithLabelValues(version, commit, goVersion).Set(1)
}

// Registry returns the underlying prometheus.Registry for the /metrics handler.
func (m *WatcherMetrics) Registry() *prometheus.Registry { return m.registry }

// GenerateLatest returns the current metrics in the Prometheus text format.
func (m *WatcherMetrics) GenerateLatest() ([]byte, error) {
	mfs, err := m.registry.Gather()
	if err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	for _, mf := range mfs {
		if _, err := expfmt.MetricFamilyToText(&buf, mf); err != nil {
			continue
		}
	}
	return buf.Bytes(), nil
}
