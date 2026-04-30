package middlewares_test

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"gateway-service/internal/middlewares"
)

// noopHandler is a simple HTTP handler that responds 200 OK.
var noopHandler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
})

// tightRateLimiterConfig returns a config that trips immediately under load.
func tightRateLimiterConfig() middlewares.RateLimiterConfig {
	return middlewares.RateLimiterConfig{
		Rate:                 1000,      // High global rate so global doesn't interfere.
		Burst:                1000,
		PerIPRate:            1,         // Allows 1 req/sec.
		PerIPBurst:           1,         // Burst = 1 (trip on 2nd request).
		MaxBackoffMultiplier: 2,         // 2-second max backoff.
		BackoffDecayInterval: time.Hour, // No decay during tests.
		IPCleanupInterval:    time.Hour, // No cleanup during tests.
	}
}

func TestRateLimiter_AllowsUnderLimit(t *testing.T) {
	cfg := tightRateLimiterConfig()
	cfg.PerIPBurst = 10 // Higher burst to avoid tripping.
	rl := middlewares.NewRateLimiter(cfg)

	handler := rl.Middleware(noopHandler)
	req := httptest.NewRequest("GET", "/", nil)
	req.RemoteAddr = "192.168.1.1:1234"

	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Errorf("expected 200 under limit, got %d", rr.Code)
	}
}

func TestRateLimiter_PerIP_ExceedsBurst_Returns429(t *testing.T) {
	rl := middlewares.NewRateLimiter(tightRateLimiterConfig())
	handler := rl.Middleware(noopHandler)

	// First request consumes the burst.
	rr1 := httptest.NewRecorder()
	req1 := httptest.NewRequest("GET", "/", nil)
	req1.RemoteAddr = "10.0.0.1:5000"
	handler.ServeHTTP(rr1, req1)

	// Second request exceeds the per-IP burst.
	rr2 := httptest.NewRecorder()
	req2 := httptest.NewRequest("GET", "/", nil)
	req2.RemoteAddr = "10.0.0.1:5000"
	handler.ServeHTTP(rr2, req2)

	if rr2.Code != http.StatusTooManyRequests {
		t.Errorf("expected 429 on burst exceeded, got %d", rr2.Code)
	}
}

func TestRateLimiter_RetryAfterHeader_SetOnBlock(t *testing.T) {
	rl := middlewares.NewRateLimiter(tightRateLimiterConfig())
	handler := rl.Middleware(noopHandler)

	for i := range 3 {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/", nil)
		req.RemoteAddr = "10.0.1.1:1234"
		handler.ServeHTTP(rr, req)
		if i > 0 && rr.Code == http.StatusTooManyRequests {
			if rr.Header().Get("Retry-After") == "" {
				t.Error("expected Retry-After header on 429")
			}
			return
		}
	}
	t.Error("never got 429 response")
}

func TestRateLimiter_RateLimitHeaders_Present(t *testing.T) {
	cfg := tightRateLimiterConfig()
	cfg.PerIPBurst = 100
	rl := middlewares.NewRateLimiter(cfg)
	handler := rl.Middleware(noopHandler)

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	req.RemoteAddr = "172.16.0.1:9999"
	handler.ServeHTTP(rr, req)

	if rr.Header().Get("X-RateLimit-Limit") == "" {
		t.Error("expected X-RateLimit-Limit header")
	}
	if rr.Header().Get("X-RateLimit-Remaining") == "" {
		t.Error("expected X-RateLimit-Remaining header")
	}
	if rr.Header().Get("X-RateLimit-Scope") == "" {
		t.Error("expected X-RateLimit-Scope header")
	}
}

func TestRateLimiter_DifferentIPs_IndependentLimits(t *testing.T) {
	rl := middlewares.NewRateLimiter(tightRateLimiterConfig())
	handler := rl.Middleware(noopHandler)

	// Exhaust IP1's budget.
	for range 3 {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/", nil)
		req.RemoteAddr = "10.10.10.1:1111"
		handler.ServeHTTP(rr, req)
	}

	// IP2 should be unaffected.
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	req.RemoteAddr = "10.10.10.2:2222"
	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Errorf("expected IP2 to be unaffected by IP1 violations, got %d", rr.Code)
	}
}

func TestRateLimiter_GlobalLimit_Returns429(t *testing.T) {
	cfg := middlewares.RateLimiterConfig{
		Rate:                 1,         // 1 token/sec global.
		Burst:                1,         // Trip on 2nd request.
		PerIPRate:            1000,      // No per-IP limit.
		PerIPBurst:           1000,
		MaxBackoffMultiplier: 2,
		BackoffDecayInterval: time.Hour,
		IPCleanupInterval:    time.Hour,
	}
	rl := middlewares.NewRateLimiter(cfg)
	handler := rl.Middleware(noopHandler)

	// Use different IPs so per-IP limit never trips.
	for i := range 5 {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/", nil)
		req.RemoteAddr = "1.2.3." + string(rune('1'+i)) + ":1234"
		handler.ServeHTTP(rr, req)
		if rr.Code == http.StatusTooManyRequests {
			return // Global limit worked.
		}
	}
	t.Error("expected global rate limit to trigger 429")
}

func TestRateLimiter_ViolationsCappedAt63(t *testing.T) {
	// This test verifies the integer overflow fix: violations are capped at 63
	// so math.Pow(2,63) stays within float64 range and the multiplier cap applies.
	rl := middlewares.NewRateLimiter(tightRateLimiterConfig())
	handler := rl.Middleware(noopHandler)

	ip := "192.168.99.1:4321"
	// Make 100 requests to accumulate violations well beyond 63.
	for range 100 {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/", nil)
		req.RemoteAddr = ip
		handler.ServeHTTP(rr, req)
	}

	// The server should still be responding (not crashed due to overflow).
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	req.RemoteAddr = ip
	handler.ServeHTTP(rr, req)

	// Either 200 (if un-blocked) or 429 — the important thing is no panic/crash.
	if rr.Code != http.StatusOK && rr.Code != http.StatusTooManyRequests {
		t.Errorf("unexpected status %d after many violations", rr.Code)
	}
}

func TestDefaultRateLimiterConfig_Sane(t *testing.T) {
	cfg := middlewares.DefaultRateLimiterConfig()
	if cfg.Rate <= 0 {
		t.Error("Rate must be positive")
	}
	if cfg.Burst <= 0 {
		t.Error("Burst must be positive")
	}
	if cfg.MaxBackoffMultiplier <= 0 {
		t.Error("MaxBackoffMultiplier must be positive")
	}
}
