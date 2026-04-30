package middlewares_test

import (
	"testing"
	"time"

	"gateway-service/internal/middlewares"
)

// newTestCB creates a circuit breaker with short durations for unit tests.
func newTestCB(t *testing.T) *middlewares.CircuitBreaker {
	t.Helper()
	return middlewares.NewCircuitBreaker(middlewares.CircuitBreakerConfig{
		FailureThreshold:   3,
		SuccessThreshold:   2,
		OpenDuration:       100 * time.Millisecond,
		HalfOpenMaxAllowed: 1,
	})
}

func tripOpen(t *testing.T, cb *middlewares.CircuitBreaker, n int) {
	t.Helper()
	for range n {
		cb.RecordFailure()
	}
}

// waitHalfOpen waits for OpenDuration to elapse so Allow() transitions Open → HalfOpen.
func waitHalfOpen(t *testing.T) {
	t.Helper()
	time.Sleep(150 * time.Millisecond)
}

// ─── State string representation ─────────────────────────────────────────────

func TestCircuitBreakerState_String(t *testing.T) {
	tests := []struct {
		state middlewares.CircuitBreakerState
		want  string
	}{
		{middlewares.CircuitClosed, "closed"},
		{middlewares.CircuitOpen, "open"},
		{middlewares.CircuitHalfOpen, "half-open"},
		{middlewares.CircuitBreakerState(99), "unknown"},
	}
	for _, tc := range tests {
		t.Run(tc.want, func(t *testing.T) {
			if got := tc.state.String(); got != tc.want {
				t.Errorf("state(%d).String() = %q, want %q", tc.state, got, tc.want)
			}
		})
	}
}

// ─── Closed state ─────────────────────────────────────────────────────────────

func TestCircuitBreaker_ClosedState(t *testing.T) {
	tests := []struct {
		name    string
		setup   func(*middlewares.CircuitBreaker)
		wantAllow bool
		wantState middlewares.CircuitBreakerState
	}{
		{
			name:      "initial state allows requests",
			setup:     func(*middlewares.CircuitBreaker) {},
			wantAllow: true,
			wantState: middlewares.CircuitClosed,
		},
		{
			name: "below threshold stays closed",
			setup: func(cb *middlewares.CircuitBreaker) {
				cb.RecordFailure()
				cb.RecordFailure() // threshold = 3, so 2 failures keep it closed
			},
			wantAllow: true,
			wantState: middlewares.CircuitClosed,
		},
		{
			name: "success in closed resets failure count",
			setup: func(cb *middlewares.CircuitBreaker) {
				cb.RecordFailure()
				cb.RecordFailure()
				cb.RecordSuccess() // resets count
				cb.RecordFailure()
				cb.RecordFailure() // only 2 since last reset
			},
			wantAllow: true,
			wantState: middlewares.CircuitClosed,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cb := newTestCB(t)
			tc.setup(cb)
			got := cb.Allow()
			if got != tc.wantAllow {
				t.Errorf("Allow() = %v, want %v", got, tc.wantAllow)
			}
			if cb.State() != tc.wantState {
				t.Errorf("State() = %v, want %v", cb.State(), tc.wantState)
			}
		})
	}
}

// ─── Closed → Open transition ─────────────────────────────────────────────────

func TestCircuitBreaker_ClosedToOpen(t *testing.T) {
	tests := []struct {
		name     string
		failures int
		wantOpen bool
	}{
		{"2 failures (below threshold=3) stays closed", 2, false},
		{"3 failures (at threshold) opens breaker", 3, true},
		{"5 failures (above threshold) opens breaker", 5, true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cb := newTestCB(t)
			tripOpen(t, cb, tc.failures)

			isOpen := cb.State() == middlewares.CircuitOpen
			if isOpen != tc.wantOpen {
				t.Errorf("after %d failures: Open=%v, want %v", tc.failures, isOpen, tc.wantOpen)
			}
			if tc.wantOpen && cb.Allow() {
				t.Error("Allow() must return false in Open state")
			}
		})
	}
}

// ─── Open → HalfOpen transition ──────────────────────────────────────────────

func TestCircuitBreaker_OpenToHalfOpen(t *testing.T) {
	cb := newTestCB(t)
	tripOpen(t, cb, 3)
	if cb.State() != middlewares.CircuitOpen {
		t.Fatal("precondition: expected Open state")
	}

	// Before OpenDuration elapses, Allow() must still block.
	if cb.Allow() {
		t.Error("Allow() returned true before OpenDuration elapsed")
	}

	waitHalfOpen(t)

	// After OpenDuration, Allow() transitions to HalfOpen and returns true.
	if !cb.Allow() {
		t.Error("Allow() returned false after OpenDuration elapsed")
	}
	if cb.State() != middlewares.CircuitHalfOpen {
		t.Errorf("State() = %v, want HalfOpen", cb.State())
	}
}

// ─── HalfOpen concurrency slot ────────────────────────────────────────────────

func TestCircuitBreaker_HalfOpen_SlotsExhausted(t *testing.T) {
	cb := newTestCB(t)
	tripOpen(t, cb, 3)
	waitHalfOpen(t)

	// First call: Open → HalfOpen, returns true.
	if !cb.Allow() {
		t.Fatal("first probe must be allowed")
	}
	// No RecordSuccess yet → slot still occupied.
	if cb.Allow() {
		t.Error("second concurrent probe must be blocked until RecordSuccess")
	}
}

// ─── HalfOpen transition ──────────────────────────────────────────────────────

// TestCircuitBreaker_HalfOpen_NoDeadlock verifies that RecordSuccess
// decrements halfOpenAllowed so subsequent probes can proceed and the
// circuit eventually closes.
func TestCircuitBreaker_HalfOpen_NoDeadlock(t *testing.T) {
	// Use SuccessThreshold=3 to ensure multiple probe round-trips are required,
	// which was exactly what caused the original deadlock.
	cfg := middlewares.CircuitBreakerConfig{
		FailureThreshold:   1,
		SuccessThreshold:   3,
		OpenDuration:       time.Millisecond,
		HalfOpenMaxAllowed: 1,
	}
	cb := middlewares.NewCircuitBreaker(cfg)
	cb.RecordFailure() // → Open
	time.Sleep(10 * time.Millisecond) // OpenDuration=1ms, sleep enough to expire it

	for i := range 3 {
		if !cb.Allow() {
			t.Fatalf("probe %d blocked (deadlock reproduced — bug not fixed)", i+1)
		}
		cb.RecordSuccess()
	}

	if cb.State() != middlewares.CircuitClosed {
		t.Errorf("after 3 successes: State() = %v, want Closed", cb.State())
	}
}

// ─── HalfOpen → Open re-trip ─────────────────────────────────────────────────

func TestCircuitBreaker_HalfOpen_FailureReopens(t *testing.T) {
	cb := newTestCB(t)
	tripOpen(t, cb, 3)
	waitHalfOpen(t)
	cb.Allow() // transition to HalfOpen

	cb.RecordFailure() // failure in HalfOpen re-opens

	if cb.State() != middlewares.CircuitOpen {
		t.Errorf("expected re-open after HalfOpen failure, got %v", cb.State())
	}
	if cb.Allow() {
		t.Error("Allow() must return false immediately after re-opening")
	}
}

// ─── Default config sanity ────────────────────────────────────────────────────

func TestDefaultCircuitBreakerConfig_Sane(t *testing.T) {
	tests := []struct {
		name  string
		check func(middlewares.CircuitBreakerConfig) bool
		msg   string
	}{
		{"FailureThreshold positive", func(c middlewares.CircuitBreakerConfig) bool { return c.FailureThreshold > 0 }, "FailureThreshold must be > 0"},
		{"SuccessThreshold positive", func(c middlewares.CircuitBreakerConfig) bool { return c.SuccessThreshold > 0 }, "SuccessThreshold must be > 0"},
		{"OpenDuration positive", func(c middlewares.CircuitBreakerConfig) bool { return c.OpenDuration > 0 }, "OpenDuration must be > 0"},
		{"HalfOpenMaxAllowed positive", func(c middlewares.CircuitBreakerConfig) bool { return c.HalfOpenMaxAllowed > 0 }, "HalfOpenMaxAllowed must be > 0"},
	}
	cfg := middlewares.DefaultCircuitBreakerConfig()
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if !tc.check(cfg) {
				t.Error(tc.msg)
			}
		})
	}
}
