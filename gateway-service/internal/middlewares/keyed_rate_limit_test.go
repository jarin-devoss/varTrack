package middlewares_test

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"gateway-service/internal/middlewares"
)

func tightKeyedConfig() middlewares.KeyedRateLimiterConfig {
	return middlewares.KeyedRateLimiterConfig{
		Rate:            1,          // 1 req/sec per key.
		Burst:           1,          // Burst = 1; 2nd req trips limit.
		CleanupInterval: time.Hour,  // No cleanup during tests.
		MaxIdleAge:      time.Hour,
	}
}

func TestKeyedRateLimiter_AllowsFirstRequest(t *testing.T) {
	krl := middlewares.NewKeyedRateLimiter(tightKeyedConfig())
	handler := krl.Middleware(noopHandler)

	req := newRequestWithDatasource("10.0.0.1:1234", "mongo")
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", rr.Code)
	}
}

func TestKeyedRateLimiter_SecondRequest_SameKey_Returns429(t *testing.T) {
	krl := middlewares.NewKeyedRateLimiter(tightKeyedConfig())
	handler := krl.Middleware(noopHandler)

	// First request consumes the burst.
	req1 := newRequestWithDatasource("10.0.0.2:1234", "mongo")
	rr1 := httptest.NewRecorder()
	handler.ServeHTTP(rr1, req1)

	// Second request from the same IP+datasource should be throttled.
	req2 := newRequestWithDatasource("10.0.0.2:1234", "mongo")
	rr2 := httptest.NewRecorder()
	handler.ServeHTTP(rr2, req2)

	if rr2.Code != http.StatusTooManyRequests {
		t.Errorf("expected 429 on 2nd request, got %d", rr2.Code)
	}
}

func TestKeyedRateLimiter_DifferentDatasources_IndependentLimits(t *testing.T) {
	krl := middlewares.NewKeyedRateLimiter(tightKeyedConfig())
	handler := krl.Middleware(noopHandler)

	ip := "10.0.0.3:9999"

	// Exhaust 'mongo' key.
	for range 3 {
		rr := httptest.NewRecorder()
		handler.ServeHTTP(rr, newRequestWithDatasource(ip, "mongo"))
	}

	// 'postgres' key should be unaffected.
	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, newRequestWithDatasource(ip, "postgres"))
	if rr.Code != http.StatusOK {
		t.Errorf("expected 'postgres' to be unaffected, got %d", rr.Code)
	}
}

func TestKeyedRateLimiter_RetryAfterHeader_Set(t *testing.T) {
	krl := middlewares.NewKeyedRateLimiter(tightKeyedConfig())
	handler := krl.Middleware(noopHandler)

	ip := "10.0.0.4:4321"
	// First: consume burst.
	rr1 := httptest.NewRecorder()
	handler.ServeHTTP(rr1, newRequestWithDatasource(ip, "mongo"))

	// Second: should be rate-limited with Retry-After.
	rr2 := httptest.NewRecorder()
	handler.ServeHTTP(rr2, newRequestWithDatasource(ip, "mongo"))

	if rr2.Code == http.StatusTooManyRequests {
		if rr2.Header().Get("Retry-After") == "" {
			t.Error("expected Retry-After header on keyed rate limit 429")
		}
	}
}

func TestKeyedRateLimiter_XRateLimitKey_Header(t *testing.T) {
	cfg := tightKeyedConfig()
	cfg.Burst = 100
	krl := middlewares.NewKeyedRateLimiter(cfg)
	handler := krl.Middleware(noopHandler)

	rr := httptest.NewRecorder()
	handler.ServeHTTP(rr, newRequestWithDatasource("10.0.0.5:1111", "redis"))

	if rr.Header().Get("X-RateLimit-Key") == "" {
		t.Error("expected X-RateLimit-Key header to be set")
	}
}

func TestDefaultKeyedRateLimiterConfig_Sane(t *testing.T) {
	cfg := middlewares.DefaultKeyedRateLimiterConfig()
	if cfg.Rate <= 0 {
		t.Error("Rate must be positive")
	}
	if cfg.Burst <= 0 {
		t.Error("Burst must be positive")
	}
	if cfg.CleanupInterval <= 0 {
		t.Error("CleanupInterval must be positive")
	}
}

// newRequestWithDatasource builds an httptest.Request with path and RemoteAddr set.
func newRequestWithDatasource(remoteAddr, datasource string) *http.Request {
	req := httptest.NewRequest("POST", "/webhooks/"+datasource, nil)
	req.RemoteAddr = remoteAddr
	// httptest.Request doesn't set path values; simulate what the mux would do.
	req.SetPathValue("datasource", datasource)
	return req
}
