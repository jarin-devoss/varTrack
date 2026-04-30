// Package monitoring provides structured logging, metrics, and tracing for
// the gateway service.
//
// Metrics design: each subsystem owns its own counters/histograms/gauges and exposes
// typed Inc/Observe/Set methods so call-sites never touch prometheus internals.
//
// All metrics share the "gw_" namespace and follow Prometheus naming conventions:
// https://prometheus.io/docs/practices/naming/
package monitoring

import (
	"net/http"
	"sync"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// GatewayMetrics is the central metrics registry for the gateway service.
// It owns all prometheus instruments and exposes a typed API that hides
// prometheus internals from callers.
type GatewayMetrics struct {
	registry *prometheus.Registry

	// ── HTTP layer ────────────────────────────────────────────────────────────

	// httpRequestsTotal counts every inbound HTTP request.
	// Labels: method, path, status_code
	httpRequestsTotal *prometheus.CounterVec

	// httpRequestDurationSeconds tracks end-to-end request latency.
	// Labels: method, path
	httpRequestDurationSeconds *prometheus.HistogramVec

	// httpRequestBodyBytes tracks inbound payload sizes.
	// Labels: path
	httpRequestBodyBytes *prometheus.HistogramVec

	// httpActiveRequests is a gauge of currently in-flight requests.
	// Labels: method, path
	httpActiveRequests *prometheus.GaugeVec

	// ── Webhook processing ────────────────────────────────────────────────────

	// webhooksTotal counts every webhook received, by outcome.
	// Labels: datasource, platform, event_type, outcome
	// outcome values: accepted | ignored | invalid_content_type | invalid_json |
	//                  invalid_signature | replay_detected | validation_failed |
	//                  datasource_not_found | orchestrator_error | circuit_open
	webhooksTotal *prometheus.CounterVec

	// webhookProcessingSeconds tracks time from receipt to orchestrator ack.
	// Labels: datasource, platform, event_type
	webhookProcessingSeconds *prometheus.HistogramVec

	// webhookPayloadBytes tracks payload size per platform.
	// Labels: platform
	webhookPayloadBytes *prometheus.HistogramVec

	// ── Orchestrator (gRPC upstream) ─────────────────────────────────────────

	// orchestratorRequestsTotal counts outbound ProcessWebhook calls.
	// Labels: rpc_method, status
	orchestratorRequestsTotal *prometheus.CounterVec

	// orchestratorRequestDurationSeconds tracks gRPC round-trip latency.
	// Labels: rpc_method
	orchestratorRequestDurationSeconds *prometheus.HistogramVec

	// orchestratorConnectionState tracks the current gRPC connection state.
	// Labels: state (idle|connecting|ready|transient_failure|shutdown)
	orchestratorConnectionState *prometheus.GaugeVec

	// ── Circuit breaker ───────────────────────────────────────────────────────

	// circuitBreakerStateChangesTotal counts state transitions.
	// Labels: from_state, to_state
	circuitBreakerStateChangesTotal *prometheus.CounterVec

	// circuitBreakerOpenTotal counts requests rejected because the breaker was open.
	circuitBreakerOpenTotal prometheus.Counter

	// circuitBreakerState is a gauge: 0=closed, 1=open, 2=half-open
	circuitBreakerState prometheus.Gauge

	// ── Rate limiting ─────────────────────────────────────────────────────────

	// rateLimitHitsTotal counts requests rejected by rate limiting.
	// Labels: limiter_type (global|per_ip|per_key), scope
	rateLimitHitsTotal *prometheus.CounterVec

	// ── Secrets / bundle ─────────────────────────────────────────────────────

	// secretResolutionsTotal counts secret fetch operations.
	// Labels: manager, outcome (hit|miss|error)
	secretResolutionsTotal *prometheus.CounterVec

	// secretCacheSize tracks the number of cached secrets.
	secretCacheSize prometheus.Gauge

	// ── Build / process info ─────────────────────────────────────────────────

	// buildInfo is a static gauge carrying version/commit labels.
	buildInfo *prometheus.GaugeVec
}

var (
	globalMetrics *GatewayMetrics
	once          sync.Once
)

// DefaultMetrics returns the singleton GatewayMetrics, creating it on first call.
// Subsequent calls return the same instance.
func DefaultMetrics() *GatewayMetrics {
	once.Do(func() {
		globalMetrics = newGatewayMetrics()
	})
	return globalMetrics
}

// newGatewayMetrics constructs and registers all instruments into a
// dedicated (non-global) prometheus registry.
func newGatewayMetrics() *GatewayMetrics {
	reg := prometheus.NewRegistry()

	// Include standard Go runtime and process collectors.
	reg.MustRegister(
		collectors.NewGoCollector(
			collectors.WithGoCollectorRuntimeMetrics(collectors.MetricsAll),
		),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)

	m := &GatewayMetrics{registry: reg}

	// ── HTTP ─────────────────────────────────────────────────────────────────

	m.httpRequestsTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_http_requests_total",
		Help: "Total number of HTTP requests received, partitioned by method, path, and status code.",
	}, []string{"method", "path", "status_code"})

	m.httpRequestDurationSeconds = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "gw_http_request_duration_seconds",
		Help:    "End-to-end HTTP request latency in seconds.",
		Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10},
	}, []string{"method", "path"})

	m.httpRequestBodyBytes = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "gw_http_request_body_bytes",
		Help:    "Inbound HTTP request body size in bytes.",
		Buckets: prometheus.ExponentialBuckets(256, 4, 10), // 256B … ~256MB
	}, []string{"path"})

	m.httpActiveRequests = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "gw_http_active_requests",
		Help: "Number of currently in-flight HTTP requests.",
	}, []string{"method", "path"})

	// ── Webhooks ─────────────────────────────────────────────────────────────

	m.webhooksTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_webhooks_total",
		Help: "Total webhooks received, partitioned by datasource, platform, event type, and outcome.",
	}, []string{"datasource", "platform", "event_type", "outcome"})

	m.webhookProcessingSeconds = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "gw_webhook_processing_seconds",
		Help:    "Time from webhook receipt to successful orchestrator acknowledgement in seconds.",
		Buckets: []float64{.01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10},
	}, []string{"datasource", "platform", "event_type"})

	m.webhookPayloadBytes = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "gw_webhook_payload_bytes",
		Help:    "Webhook payload size in bytes per platform.",
		Buckets: prometheus.ExponentialBuckets(512, 2, 14), // 512B … 4MB+
	}, []string{"platform"})

	// ── Orchestrator ─────────────────────────────────────────────────────────

	m.orchestratorRequestsTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_orchestrator_requests_total",
		Help: "Total outbound gRPC calls to the orchestrator, partitioned by RPC method and status.",
	}, []string{"rpc_method", "status"})

	m.orchestratorRequestDurationSeconds = prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "gw_orchestrator_request_duration_seconds",
		Help:    "Outbound gRPC round-trip latency in seconds.",
		Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5},
	}, []string{"rpc_method"})

	m.orchestratorConnectionState = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "gw_orchestrator_connection_state",
		Help: "Current gRPC connection state to the orchestrator (1 = active state, 0 = inactive).",
	}, []string{"state"})

	// ── Circuit breaker ───────────────────────────────────────────────────────

	m.circuitBreakerStateChangesTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_circuit_breaker_state_changes_total",
		Help: "Total circuit breaker state transitions.",
	}, []string{"from_state", "to_state"})

	m.circuitBreakerOpenTotal = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "gw_circuit_breaker_open_rejections_total",
		Help: "Total requests rejected because the circuit breaker was open.",
	})

	m.circuitBreakerState = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "gw_circuit_breaker_state",
		Help: "Current circuit breaker state: 0=closed, 1=open, 2=half-open.",
	})

	// ── Rate limiting ─────────────────────────────────────────────────────────

	m.rateLimitHitsTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_rate_limit_hits_total",
		Help: "Total requests rejected by the rate limiter, by limiter type and scope.",
	}, []string{"limiter_type", "scope"})

	// ── Secrets ──────────────────────────────────────────────────────────────

	m.secretResolutionsTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "gw_secret_resolutions_total",
		Help: "Total secret resolution operations, partitioned by manager and outcome (hit|miss|error).",
	}, []string{"manager", "outcome"})

	m.secretCacheSize = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "gw_secret_cache_size",
		Help: "Number of secrets currently held in the TTL cache.",
	})

	// ── Build info ────────────────────────────────────────────────────────────

	m.buildInfo = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "gw_build_info",
		Help: "Gateway service build information. Always 1.",
	}, []string{"version", "commit", "go_version"})

	// Register everything.
	reg.MustRegister(
		m.httpRequestsTotal,
		m.httpRequestDurationSeconds,
		m.httpRequestBodyBytes,
		m.httpActiveRequests,
		m.webhooksTotal,
		m.webhookProcessingSeconds,
		m.webhookPayloadBytes,
		m.orchestratorRequestsTotal,
		m.orchestratorRequestDurationSeconds,
		m.orchestratorConnectionState,
		m.circuitBreakerStateChangesTotal,
		m.circuitBreakerOpenTotal,
		m.circuitBreakerState,
		m.rateLimitHitsTotal,
		m.secretResolutionsTotal,
		m.secretCacheSize,
		m.buildInfo,
	)

	return m
}

// Handler returns an http.Handler that serves the /metrics endpoint.
func (m *GatewayMetrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{
		EnableOpenMetrics: true,
	})
}

// ── HTTP methods ─────────────────────────────────────────────────────────────

// IncHTTPRequest records a completed HTTP request.
func (m *GatewayMetrics) IncHTTPRequest(method, path, statusCode string) {
	m.httpRequestsTotal.WithLabelValues(method, path, statusCode).Inc()
}

// ObserveHTTPDuration records request latency.
func (m *GatewayMetrics) ObserveHTTPDuration(method, path string, seconds float64) {
	m.httpRequestDurationSeconds.WithLabelValues(method, path).Observe(seconds)
}

// ObserveHTTPBodyBytes records inbound payload size.
func (m *GatewayMetrics) ObserveHTTPBodyBytes(path string, bytes float64) {
	m.httpRequestBodyBytes.WithLabelValues(path).Observe(bytes)
}

// IncActiveRequests increments the in-flight gauge. Callers must pair it
// with a deferred DecActiveRequests.
func (m *GatewayMetrics) IncActiveRequests(method, path string) {
	m.httpActiveRequests.WithLabelValues(method, path).Inc()
}

// DecActiveRequests decrements the in-flight gauge.
func (m *GatewayMetrics) DecActiveRequests(method, path string) {
	m.httpActiveRequests.WithLabelValues(method, path).Dec()
}

// ── Webhook methods ───────────────────────────────────────────────────────────

// IncWebhook records a webhook with its final outcome.
func (m *GatewayMetrics) IncWebhook(datasource, platform, eventType, outcome string) {
	m.webhooksTotal.WithLabelValues(datasource, platform, eventType, outcome).Inc()
}

// ObserveWebhookProcessing records the end-to-end processing time for a
// successfully accepted webhook.
func (m *GatewayMetrics) ObserveWebhookProcessing(datasource, platform, eventType string, seconds float64) {
	m.webhookProcessingSeconds.WithLabelValues(datasource, platform, eventType).Observe(seconds)
}

// ObserveWebhookPayload records payload size by platform.
func (m *GatewayMetrics) ObserveWebhookPayload(platform string, bytes float64) {
	m.webhookPayloadBytes.WithLabelValues(platform).Observe(bytes)
}

// ── Orchestrator methods ──────────────────────────────────────────────────────

// IncOrchestratorRequest records an outbound gRPC call.
func (m *GatewayMetrics) IncOrchestratorRequest(rpcMethod, status string) {
	m.orchestratorRequestsTotal.WithLabelValues(rpcMethod, status).Inc()
}

// ObserveOrchestratorDuration records gRPC round-trip latency.
func (m *GatewayMetrics) ObserveOrchestratorDuration(rpcMethod string, seconds float64) {
	m.orchestratorRequestDurationSeconds.WithLabelValues(rpcMethod).Observe(seconds)
}

// SetOrchestratorConnectionState updates the connection state gauge.
// It sets the active state to 1 and all others to 0.
func (m *GatewayMetrics) SetOrchestratorConnectionState(state string) {
	for _, s := range []string{"idle", "connecting", "ready", "transient_failure", "shutdown"} {
		v := float64(0)
		if s == state {
			v = 1
		}
		m.orchestratorConnectionState.WithLabelValues(s).Set(v)
	}
}

// ── Circuit breaker methods ───────────────────────────────────────────────────

// IncCircuitBreakerStateChange records a state transition.
func (m *GatewayMetrics) IncCircuitBreakerStateChange(fromState, toState string) {
	m.circuitBreakerStateChangesTotal.WithLabelValues(fromState, toState).Inc()
}

// IncCircuitBreakerOpenRejection records a request that was fast-failed.
func (m *GatewayMetrics) IncCircuitBreakerOpenRejection() {
	m.circuitBreakerOpenTotal.Inc()
}

// SetCircuitBreakerState sets the numeric state gauge.
// 0 = closed, 1 = open, 2 = half-open.
func (m *GatewayMetrics) SetCircuitBreakerState(state float64) {
	m.circuitBreakerState.Set(state)
}

// ── Rate limit methods ────────────────────────────────────────────────────────

// IncRateLimitHit records a rejected request.
// limiterType: "global" | "per_ip" | "per_key"
func (m *GatewayMetrics) IncRateLimitHit(limiterType, scope string) {
	m.rateLimitHitsTotal.WithLabelValues(limiterType, scope).Inc()
}

// ── Secret methods ────────────────────────────────────────────────────────────

// IncSecretResolution records a secret fetch operation.
// outcome: "hit" (cache) | "miss" (fetched) | "error"
func (m *GatewayMetrics) IncSecretResolution(manager, outcome string) {
	m.secretResolutionsTotal.WithLabelValues(manager, outcome).Inc()
}

// SetSecretCacheSize updates the cache size gauge.
func (m *GatewayMetrics) SetSecretCacheSize(n float64) {
	m.secretCacheSize.Set(n)
}

// ── Build info ────────────────────────────────────────────────────────────────

// SetBuildInfo sets the static build-info gauge. Call once at startup.
func (m *GatewayMetrics) SetBuildInfo(version, commit, goVersion string) {
	m.buildInfo.WithLabelValues(version, commit, goVersion).Set(1)
}
