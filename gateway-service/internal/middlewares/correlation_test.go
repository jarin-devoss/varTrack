package middlewares_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"gateway-service/internal/middlewares"
)

func TestCorrelationID_GeneratedWhenAbsent(t *testing.T) {
	handler := middlewares.CorrelationID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	handler.ServeHTTP(rr, req)

	id := rr.Header().Get(middlewares.HeaderCorrelationID)
	if id == "" {
		t.Error("expected X-Correlation-ID to be generated when not provided")
	}
}

func TestCorrelationID_PreservesExistingID(t *testing.T) {
	existing := "my-upstream-trace-id-12345"
	handler := middlewares.CorrelationID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set(middlewares.HeaderCorrelationID, existing)
	handler.ServeHTTP(rr, req)

	got := rr.Header().Get(middlewares.HeaderCorrelationID)
	if got != existing {
		t.Errorf("expected correlation ID %q to be preserved, got %q", existing, got)
	}
}

func TestCorrelationID_TruncatesOverlongID(t *testing.T) {
	// 200-char ID should be truncated to 128.
	longID := strings.Repeat("a", 200)
	handler := middlewares.CorrelationID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set(middlewares.HeaderCorrelationID, longID)
	handler.ServeHTTP(rr, req)

	got := rr.Header().Get(middlewares.HeaderCorrelationID)
	if len(got) != 128 {
		t.Errorf("expected truncated length 128, got %d", len(got))
	}
	if got != longID[:128] {
		t.Errorf("truncated ID mismatch: got %q", got)
	}
}

func TestCorrelationID_ExactlyMaxLength_NotTruncated(t *testing.T) {
	// Exactly 128 chars should NOT be truncated.
	id128 := strings.Repeat("b", 128)
	handler := middlewares.CorrelationID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set(middlewares.HeaderCorrelationID, id128)
	handler.ServeHTTP(rr, req)

	got := rr.Header().Get(middlewares.HeaderCorrelationID)
	if got != id128 {
		t.Errorf("128-char ID should not be truncated, got len=%d", len(got))
	}
}

func TestCorrelationID_StoredInContext(t *testing.T) {
	expected := "trace-abc-123"
	var contextID string

	handler := middlewares.CorrelationID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		contextID = middlewares.GetCorrelationID(r.Context())
	}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set(middlewares.HeaderCorrelationID, expected)
	handler.ServeHTTP(rr, req)

	if contextID != expected {
		t.Errorf("expected context ID %q, got %q", expected, contextID)
	}
}

func TestGetCorrelationID_MissingContext(t *testing.T) {
	req := httptest.NewRequest("GET", "/", nil)
	id := middlewares.GetCorrelationID(req.Context())
	if id != "" {
		t.Errorf("expected empty string for missing correlation ID, got %q", id)
	}
}
