package middlewares_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"gateway-service/internal/middlewares"
)

func TestRequestID_HeaderSetOnResponse(t *testing.T) {
	handler := middlewares.RequestID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	handler.ServeHTTP(rr, req)

	if rr.Header().Get(middlewares.HeaderRequestID) == "" {
		t.Error("expected X-Request-ID header to be set")
	}
}

func TestRequestID_UniqueAcrossRequests(t *testing.T) {
	handler := middlewares.RequestID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	ids := make(map[string]struct{}, 200)
	for i := 0; i < 200; i++ {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/", nil)
		handler.ServeHTTP(rr, req)

		id := rr.Header().Get(middlewares.HeaderRequestID)
		if _, dup := ids[id]; dup {
			t.Fatalf("duplicate request ID at iteration %d: %q", i, id)
		}
		ids[id] = struct{}{}
	}
}

func TestRequestID_ConcurrentUniqueness(t *testing.T) {
	handler := middlewares.RequestID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	const goroutines = 50
	results := make([]string, goroutines)
	var wg sync.WaitGroup
	wg.Add(goroutines)

	for i := range goroutines {
		go func(i int) {
			defer wg.Done()
			rr := httptest.NewRecorder()
			req := httptest.NewRequest("GET", "/", nil)
			handler.ServeHTTP(rr, req)
			results[i] = rr.Header().Get(middlewares.HeaderRequestID)
		}(i)
	}
	wg.Wait()

	seen := make(map[string]struct{}, goroutines)
	for _, id := range results {
		if _, dup := seen[id]; dup {
			t.Fatalf("concurrent duplicate request ID: %q", id)
		}
		seen[id] = struct{}{}
	}
}

func TestRequestID_PrefixCounterFormat(t *testing.T) {
	handler := middlewares.RequestID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	handler.ServeHTTP(rr, req)

	id := rr.Header().Get(middlewares.HeaderRequestID)
	// Format: <12-hex-char-prefix>-<base36-counter>
	parts := strings.SplitN(id, "-", 2)
	if len(parts) != 2 {
		t.Fatalf("expected <prefix>-<counter> format, got %q", id)
	}
	// 6 bytes hex-encoded = 12 chars.
	if len(parts[0]) != 12 {
		t.Errorf("expected 12-char hex prefix, got %d chars: %q", len(parts[0]), parts[0])
	}
	if parts[1] == "" {
		t.Errorf("expected non-empty counter in %q", id)
	}
}

func TestRequestID_StoredInContext(t *testing.T) {
	var contextID string
	handler := middlewares.RequestID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		contextID = middlewares.GetRequestID(r.Context())
	}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	handler.ServeHTTP(rr, req)

	headerID := rr.Header().Get(middlewares.HeaderRequestID)
	if contextID == "" {
		t.Error("request ID not found in context")
	}
	if contextID != headerID {
		t.Errorf("context ID %q != header ID %q", contextID, headerID)
	}
}

func TestGetRequestID_MissingContext(t *testing.T) {
	req := httptest.NewRequest("GET", "/", nil)
	id := middlewares.GetRequestID(req.Context())
	if id != "" {
		t.Errorf("expected empty string for missing request ID, got %q", id)
	}
}
