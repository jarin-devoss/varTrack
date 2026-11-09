package handlers_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"gateway-service/internal/middlewares"
	"gateway-service/internal/routes"
)

// flakyNonces is a store that can be told to be down: what Redis is when it is stopped.
type flakyNonces struct {
	mu   sync.Mutex
	down bool
	seen map[string]bool
}

func (f *flakyNonces) CheckAndSet(nonce string, _ time.Duration) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.down {
		return false, middlewares.ErrNonceStoreUnavailable
	}
	if f.seen == nil {
		f.seen = map[string]bool{}
	}
	if f.seen[nonce] {
		return false, nil
	}
	f.seen[nonce] = true
	return true, nil
}

func deliverNonce(t *testing.T, store middlewares.NonceStore, delivery string) (*recorder, *httptest.ResponseRecorder) {
	t.Helper()
	bundle := twoGitHubs("public-secret", "")
	orch := &recorder{}
	h := routes.WebhookRoutes(bundle, orch, middlewares.NewCircuitBreaker(middlewares.DefaultCircuitBreakerConfig()), store, true)
	req := httptest.NewRequest(http.MethodPost, "/mongo", strings.NewReader(push))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Event", "push")
	req.Header.Set("X-GitHub-Delivery", delivery)
	req.Header.Set("X-Hub-Signature-256", sign("public-secret", push))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return orch, rec
}

func TestAWebhookIsRefusedWhenItsDeliveryCannotBeChecked(t *testing.T) {
	store := &flakyNonces{down: true}
	orch, rec := deliverNonce(t, store, "delivery-1")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503 (body %s)", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "GW_REPLAY_PROTECTION_UNAVAILABLE") || rec.Header().Get("Retry-After") == "" {
		t.Fatalf("the refusal must name the cause and tell the sender to retry: %s %v", rec.Body.String(), rec.Header())
	}
	if len(orch.forwarded) != 0 {
		t.Fatal("a webhook whose delivery could not be checked reached the orchestrator")
	}
}

func TestTheSameDeliveryIsAcceptedOnceAndThenOnceTheStoreIsBack(t *testing.T) {
	store := &flakyNonces{down: true}
	if _, rec := deliverNonce(t, store, "delivery-2"); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("while down: %d", rec.Code)
	}
	store.down = false
	// the refused delivery was never recorded, so the sender's retry is the first time it is seen
	if orch, rec := deliverNonce(t, store, "delivery-2"); rec.Code != http.StatusAccepted || len(orch.forwarded) != 1 {
		t.Fatalf("retry: %d, calls=%d (%s)", rec.Code, len(orch.forwarded), rec.Body.String())
	}
	if _, rec := deliverNonce(t, store, "delivery-2"); rec.Code != http.StatusUnauthorized {
		t.Fatalf("a replay with the store up must still be refused: %d", rec.Code)
	}
}
