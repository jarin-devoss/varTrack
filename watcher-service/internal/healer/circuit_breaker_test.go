package healer

import (
	"testing"
	"time"
)

// ── helpers ────────────────────────────────────────────────────────────────────

// newFastBreaker returns a circuit breaker with low thresholds for testing.
func newFastBreaker() *circuitBreaker {
	cb := newCircuitBreaker()
	cb.failureThreshold = 3
	cb.successThreshold = 2
	cb.openDuration = 30 * time.Millisecond
	cb.halfOpenMaxAllowed = 1
	return cb
}

// tripBreaker drives the breaker from Closed → Open.
func tripBreaker(cb *circuitBreaker) {
	for i := 0; i < cb.failureThreshold; i++ {
		cb.recordFailure()
	}
}

// ── state transition tests ─────────────────────────────────────────────────────

func TestCircuitBreaker_closedAllows(t *testing.T) {
	cb := newFastBreaker()
	if !cb.allow() {
		t.Error("closed breaker must allow requests")
	}
}

func TestCircuitBreaker_opensAfterFailureThreshold(t *testing.T) {
	cb := newFastBreaker()
	tripBreaker(cb)

	if cb.state != circuitOpen {
		t.Errorf("expected Open, got %s", cb.state)
	}
	if cb.allow() {
		t.Error("open breaker must reject requests")
	}
}

func TestCircuitBreaker_halfOpenAfterOpenDuration(t *testing.T) {
	cb := newFastBreaker()
	tripBreaker(cb)

	// Wait for openDuration to elapse.
	time.Sleep(cb.openDuration + 10*time.Millisecond)

	if !cb.allow() {
		t.Error("breaker should allow one probe in half-open")
	}
	if cb.state != circuitHalfOpen {
		t.Errorf("expected HalfOpen after timeout, got %s", cb.state)
	}
}

func TestCircuitBreaker_halfOpenBlocksExtraProbes(t *testing.T) {
	cb := newFastBreaker()
	tripBreaker(cb)
	time.Sleep(cb.openDuration + 10*time.Millisecond)

	// First probe allowed.
	if !cb.allow() {
		t.Fatal("first half-open probe must be allowed")
	}
	// Second probe must be blocked while slot is taken.
	if cb.allow() {
		t.Error("second half-open probe must be blocked (halfOpenMaxAllowed=1)")
	}
}

func TestCircuitBreaker_closesAfterSuccessThreshold(t *testing.T) {
	cb := newFastBreaker()
	tripBreaker(cb)
	time.Sleep(cb.openDuration + 10*time.Millisecond)

	// Drive through HalfOpen → Closed via repeated probe + success.
	for i := 0; i < cb.successThreshold; i++ {
		ok := cb.allow()
		if !ok {
			// After a success decrements halfOpenAllowed, next allow() returns true.
			// This is expected; break only if we actually get stuck.
		}
		cb.recordSuccess()
	}

	if cb.state != circuitClosed {
		t.Errorf("expected Closed after %d successes, got %s", cb.successThreshold, cb.state)
	}
}

func TestCircuitBreaker_halfOpenFailureReopens(t *testing.T) {
	cb := newFastBreaker()
	tripBreaker(cb)
	time.Sleep(cb.openDuration + 10*time.Millisecond)

	cb.allow() // enter half-open
	cb.recordFailure()

	if cb.state != circuitOpen {
		t.Errorf("expected Open after half-open failure, got %s", cb.state)
	}
}

func TestCircuitBreaker_successInClosedResetsFailureCount(t *testing.T) {
	cb := newFastBreaker()

	// Accumulate failures below threshold.
	for i := 0; i < cb.failureThreshold-1; i++ {
		cb.recordFailure()
	}
	cb.recordSuccess() // resets failure count

	// One more failure must not trip (count was reset).
	cb.recordFailure()
	if cb.state == circuitOpen {
		t.Error("breaker must not open: failure count was reset by success")
	}
}

func TestCircuitBreaker_stateStringValues(t *testing.T) {
	cases := []struct {
		state circuitState
		want  string
	}{
		{circuitClosed, "closed"},
		{circuitOpen, "open"},
		{circuitHalfOpen, "half-open"},
		{circuitState(99), "unknown"},
	}
	for _, tc := range cases {
		if got := tc.state.String(); got != tc.want {
			t.Errorf("state %d: got %q, want %q", tc.state, got, tc.want)
		}
	}
}
