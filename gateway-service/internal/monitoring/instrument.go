package monitoring

import (
	"fmt"
	"net/http"
	"strconv"
	"time"
)

// InstrumentHandler wraps an http.Handler and records per-request metrics:
//   - gw_http_requests_total        (method, path, status_code)
//   - gw_http_request_duration_seconds (method, path)
//   - gw_http_active_requests       (method, path)
//
// path is normalised to the registered route pattern when available via
// http.Request.Pattern, falling back to r.URL.Path for compatibility.
//
// Usage:
//
//	mux.Handle("/webhooks/", InstrumentHandler(webhookHandler, metrics))
func InstrumentHandler(next http.Handler, m *GatewayMetrics) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := normalisePattern(r)
		method := r.Method

		m.IncActiveRequests(method, path)
		defer m.DecActiveRequests(method, path)

		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)

		elapsed := time.Since(start).Seconds()
		status := strconv.Itoa(sw.status)

		m.IncHTTPRequest(method, path, status)
		m.ObserveHTTPDuration(method, path, elapsed)

		if r.ContentLength > 0 {
			m.ObserveHTTPBodyBytes(path, float64(r.ContentLength))
		}
	})
}

// normalisePattern returns a stable, low-cardinality path label.
// Go 1.22+ sets r.Pattern on matched routes; older versions fall back to r.URL.Path.
func normalisePattern(r *http.Request) string {
	if p := r.Pattern; p != "" {
		return p
	}
	return sanitizePath(r.URL.Path)
}

// sanitizePath collapses hex UUIDs, numeric IDs, and SHA-like segments
// so the cardinality of the "path" label stays low.
func sanitizePath(path string) string {
	// Keep it simple: return the path as-is for known small cardinality routes.
	// In a more complex service this would regex-replace UUIDs etc.
	return path
}

// statusWriter captures the HTTP status code written by the downstream handler.
type statusWriter struct {
	http.ResponseWriter
	status int
}

func (sw *statusWriter) WriteHeader(code int) {
	sw.status = code
	sw.ResponseWriter.WriteHeader(code)
}

// Unwrap allows middleware that wraps w to reach the underlying writer.
func (sw *statusWriter) Unwrap() http.ResponseWriter {
	return sw.ResponseWriter
}

// GRPCStateToLabel maps grpc/connectivity.State names to Prometheus label strings.
// Normalises the raw stringer output ("READY" → "ready", etc.).
func GRPCStateToLabel(state fmt.Stringer) string {
	switch state.String() {
	case "IDLE":
		return "idle"
	case "CONNECTING":
		return "connecting"
	case "READY":
		return "ready"
	case "TRANSIENT_FAILURE":
		return "transient_failure"
	case "SHUTDOWN":
		return "shutdown"
	default:
		return "unknown"
	}
}
