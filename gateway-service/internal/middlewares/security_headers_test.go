package middlewares_test

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"gateway-service/internal/middlewares"
)

func TestSecurityHeaders_AllPresent(t *testing.T) {
	handler := middlewares.SecurityHeaders(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	handler.ServeHTTP(rr, req)

	checks := map[string]string{
		"X-Content-Type-Options": "nosniff",
		"X-Frame-Options":        "DENY",
		"Cache-Control":          "no-store, no-cache, must-revalidate",
		"Expires":                "0",
		"Pragma":                 "no-cache",
	}
	for header, want := range checks {
		got := rr.Header().Get(header)
		if got != want {
			t.Errorf("header %q = %q, want %q", header, got, want)
		}
	}
}

func TestSecurityHeaders_DownstreamHandlerStillCalled(t *testing.T) {
	called := false
	handler := middlewares.SecurityHeaders(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusAccepted)
	}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/webhook", nil)
	handler.ServeHTTP(rr, req)

	if !called {
		t.Error("expected downstream handler to be called")
	}
	if rr.Code != http.StatusAccepted {
		t.Errorf("expected 202 from downstream, got %d", rr.Code)
	}
}
