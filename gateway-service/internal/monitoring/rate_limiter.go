package monitoring

import (
	"net/http"

	"gateway-service/internal/middlewares"
)

// InstrumentedRateLimiter wraps a *middlewares.RateLimiter and emits
// gw_rate_limit_hits_total on every rejected request.
type InstrumentedRateLimiter struct {
	inner   *middlewares.RateLimiter
	metrics *GatewayMetrics
}

// NewInstrumentedRateLimiter wraps rl with metrics emission on rejection.
func NewInstrumentedRateLimiter(rl *middlewares.RateLimiter, m *GatewayMetrics) *InstrumentedRateLimiter {
	return &InstrumentedRateLimiter{inner: rl, metrics: m}
}

// Middleware returns an http.Handler middleware that rate-limits requests and
// increments the metric counter when a request is rejected.
func (r *InstrumentedRateLimiter) Middleware(next http.Handler) http.Handler {
	// Wrap the inner middleware in a detector: we detect a 429 response and
	// increment the metric before passing the response along.
	inner := r.inner.Middleware(next)
	m := r.metrics

	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		sw := &rateLimitStatusWriter{ResponseWriter: w}
		inner.ServeHTTP(sw, req)
		if sw.rejected {
			m.IncRateLimitHit("per_ip", "ip:"+getIP(req))
		}
	})
}

// InstrumentedKeyedRateLimiter wraps *middlewares.KeyedRateLimiter.
type InstrumentedKeyedRateLimiter struct {
	inner   *middlewares.KeyedRateLimiter
	metrics *GatewayMetrics
}

// NewInstrumentedKeyedRateLimiter wraps krl with metrics emission on rejection.
func NewInstrumentedKeyedRateLimiter(krl *middlewares.KeyedRateLimiter, m *GatewayMetrics) *InstrumentedKeyedRateLimiter {
	return &InstrumentedKeyedRateLimiter{inner: krl, metrics: m}
}

// Middleware returns an http.Handler middleware that rate-limits by key and
// increments the metric counter when a request is rejected.
func (r *InstrumentedKeyedRateLimiter) Middleware(next http.Handler) http.Handler {
	inner := r.inner.Middleware(next)
	m := r.metrics

	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		sw := &rateLimitStatusWriter{ResponseWriter: w}
		inner.ServeHTTP(sw, req)
		if sw.rejected {
			key := req.Header.Get("X-RateLimit-Key")
			if key == "" {
				key = "unknown"
			}
			m.IncRateLimitHit("per_key", key)
		}
	})
}

// rateLimitStatusWriter detects 429 responses so we can emit the metric.
type rateLimitStatusWriter struct {
	http.ResponseWriter
	rejected bool
}

func (sw *rateLimitStatusWriter) WriteHeader(code int) {
	if code == http.StatusTooManyRequests {
		sw.rejected = true
	}
	sw.ResponseWriter.WriteHeader(code)
}

func getIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		return xff
	}
	return r.RemoteAddr
}
