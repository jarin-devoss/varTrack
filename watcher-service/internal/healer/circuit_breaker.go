package healer

import (
	"log/slog"
	"sync"
	"time"
)

// circuitState is the current state of the circuit breaker.
type circuitState int

const (
	circuitClosed   circuitState = iota // Normal: requests flow through.
	circuitOpen                         // Tripped: requests fail fast.
	circuitHalfOpen                     // Probing: limited requests allowed.
)

func (s circuitState) String() string {
	switch s {
	case circuitClosed:
		return "closed"
	case circuitOpen:
		return "open"
	case circuitHalfOpen:
		return "half-open"
	default:
		return "unknown"
	}
}

// circuitBreaker protects the healer from hammering a down orchestrator.
// Mirrors gateway-service/internal/middlewares/circuit_breaker.go exactly,
// including the HalfOpen deadlock fix in RecordSuccess.
type circuitBreaker struct {
	mu              sync.Mutex
	state           circuitState
	failureCount    int
	successCount    int
	halfOpenAllowed int
	lastFailureTime time.Time

	failureThreshold   int
	successThreshold   int
	openDuration       time.Duration
	halfOpenMaxAllowed int
}

func newCircuitBreaker() *circuitBreaker {
	return &circuitBreaker{
		state:              circuitClosed,
		failureThreshold:   5,
		successThreshold:   3,
		openDuration:       30 * time.Second,
		halfOpenMaxAllowed: 1,
	}
}

// allow returns true if the request should proceed.
func (cb *circuitBreaker) allow() bool {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case circuitClosed:
		return true

	case circuitOpen:
		if time.Since(cb.lastFailureTime) > cb.openDuration {
			cb.transitionTo(circuitHalfOpen)
			cb.halfOpenAllowed = 1
			return true
		}
		return false

	case circuitHalfOpen:
		if cb.halfOpenAllowed < cb.halfOpenMaxAllowed {
			cb.halfOpenAllowed++
			return true
		}
		return false

	default:
		return true
	}
}

// recordSuccess records a successful request.
func (cb *circuitBreaker) recordSuccess() {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case circuitHalfOpen:
		// Decrement halfOpenAllowed so the next probe slot opens.
		// Without this, successCount can never reach successThreshold
		// because allow() blocks all further probes once the slot fills.
		cb.halfOpenAllowed--
		cb.successCount++
		if cb.successCount >= cb.successThreshold {
			cb.transitionTo(circuitClosed)
		}
	case circuitClosed:
		cb.failureCount = 0
	}
}

// recordFailure records a failed request.
func (cb *circuitBreaker) recordFailure() {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case circuitClosed:
		cb.failureCount++
		if cb.failureCount >= cb.failureThreshold {
			cb.lastFailureTime = time.Now()
			cb.transitionTo(circuitOpen)
		}
	case circuitHalfOpen:
		cb.lastFailureTime = time.Now()
		cb.transitionTo(circuitOpen)
	}
}

// transitionTo moves to a new state and resets counters.
// Must be called with cb.mu held.
func (cb *circuitBreaker) transitionTo(next circuitState) {
	prev := cb.state
	cb.state = next
	cb.failureCount = 0
	cb.successCount = 0
	cb.halfOpenAllowed = 0
	slog.Info("healer: circuit breaker state change",
		"from", prev.String(),
		"to", next.String(),
	)
}
