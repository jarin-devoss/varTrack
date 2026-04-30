package middlewares_test

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"gateway-service/internal/middlewares"
)

func TestRecovery_CatchesPanic_Returns500(t *testing.T) {
	panicHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		panic("test panic — intentional")
	})

	handler := middlewares.Recovery()(panicHandler)
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)

	// Must not propagate the panic.
	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusInternalServerError {
		t.Errorf("expected 500 after panic, got %d", rr.Code)
	}
}

func TestRecovery_NoopWhenNoPanic(t *testing.T) {
	handler := middlewares.Recovery()(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusAccepted)
	}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusAccepted {
		t.Errorf("expected 202 when no panic, got %d", rr.Code)
	}
}

func TestRecovery_NilPanic_Returns500(t *testing.T) {
	handler := middlewares.Recovery()(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		panic(nil)
	}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)

	handler.ServeHTTP(rr, req)

	// A nil panic is still a panic — 500 expected.
	if rr.Code != http.StatusInternalServerError {
		t.Errorf("expected 500 for nil panic, got %d", rr.Code)
	}
}

func TestRecovery_DoesNotDoubleWriteHeaders(t *testing.T) {
	// Handler writes a header before panicking — recovery must not overwrite it.
	handler := middlewares.Recovery()(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		panic("panic after headers sent")
	}))

	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/", nil)
	handler.ServeHTTP(rr, req)

	// Status should remain 200 (the first write), not get overwritten to 500.
	if rr.Code != http.StatusOK {
		t.Errorf("expected original 200 to be preserved, got %d", rr.Code)
	}
}

// mockFlusher is a ResponseRecorder that also implements http.Flusher.
type mockFlusher struct {
	*httptest.ResponseRecorder
	flushed bool
}

func (f *mockFlusher) Flush() {
	f.flushed = true
}

func TestRecovery_Flush_DelegatesToUnderlying(t *testing.T) {
	// The recoverResponseWriter wraps the underlying ResponseWriter.
	// When the underlying implements http.Flusher, Flush() must delegate.
	mf := &mockFlusher{ResponseRecorder: httptest.NewRecorder()}

	handler := middlewares.Recovery()(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		} else {
			t.Error("expected recoverResponseWriter to implement http.Flusher")
		}
	}))

	req := httptest.NewRequest("GET", "/", nil)
	handler.ServeHTTP(mf, req)

	if !mf.flushed {
		t.Error("expected Flush() to have been called on underlying writer")
	}
}
