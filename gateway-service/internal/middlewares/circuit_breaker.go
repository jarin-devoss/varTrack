// Package middlewares provides HTTP middleware for the gateway service,
// including rate limiting, circuit breaking, request logging, and security.
package middlewares

import (
	"log/slog"
	"sync"
	"time"
)

// CircuitBreakerState represents the three states of the circuit breaker.
type CircuitBreakerState int

const (
	CircuitClosed   CircuitBreakerState = iota // Normal: requests flow through.
	CircuitOpen                                // Tripped: requests fail fast.
	CircuitHalfOpen                            // Probing: limited requests allowed.
)

func (s CircuitBreakerState) String() string {
	switch s {
	case CircuitClosed:
		return "closed"
	case CircuitOpen:
		return "open"
	case CircuitHalfOpen:
		return "half-open"
	default:
		return "unknown"
	}
}

// CircuitBreakerConfig configures the circuit breaker thresholds.
type CircuitBreakerConfig struct {
	FailureThreshold   int           // Consecutive failures to trip the breaker.
	SuccessThreshold   int           // Consecutive successes in half-open to close.
	OpenDuration       time.Duration // How long to stay open before probing.
	HalfOpenMaxAllowed int           // Max concurrent requests in half-open state.
}

// DefaultCircuitBreakerConfig returns production-ready defaults.
func DefaultCircuitBreakerConfig() CircuitBreakerConfig {
	return CircuitBreakerConfig{
		FailureThreshold:   5,
		SuccessThreshold:   3,
		OpenDuration:       30 * time.Second,
		HalfOpenMaxAllowed: 1,
	}
}

// CircuitBreaker protects against cascading failures when the orchestrator
// backend is unresponsive.
type CircuitBreaker struct {
	mu              sync.Mutex
	state           CircuitBreakerState
	failureCount    int
	successCount    int
	halfOpenAllowed int
	lastFailureTime time.Time
	config          CircuitBreakerConfig
}

// NewCircuitBreaker creates a new CircuitBreaker.
func NewCircuitBreaker(cfg CircuitBreakerConfig) *CircuitBreaker {
	return &CircuitBreaker{
		state:  CircuitClosed,
		config: cfg,
	}
}

// Allow returns true if the request should be allowed.
func (cb *CircuitBreaker) Allow() bool {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case CircuitClosed:
		return true

	case CircuitOpen:
		if time.Since(cb.lastFailureTime) > cb.config.OpenDuration {
			cb.transitionTo(CircuitHalfOpen)
			cb.halfOpenAllowed = 1
			return true
		}
		return false

	case CircuitHalfOpen:
		if cb.halfOpenAllowed < cb.config.HalfOpenMaxAllowed {
			cb.halfOpenAllowed++
			return true
		}
		return false

	default:
		return true
	}
}

// RecordSuccess records a successful request.
func (cb *CircuitBreaker) RecordSuccess() {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case CircuitHalfOpen:
		cb.halfOpenAllowed--
		cb.successCount++
		if cb.successCount >= cb.config.SuccessThreshold {
			cb.transitionTo(CircuitClosed)
		}
	case CircuitClosed:
		cb.failureCount = 0
	}
}

// RecordFailure records a failed request.
func (cb *CircuitBreaker) RecordFailure() {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case CircuitClosed:
		cb.failureCount++
		if cb.failureCount >= cb.config.FailureThreshold {
			cb.lastFailureTime = time.Now()
			cb.transitionTo(CircuitOpen)
		}
	case CircuitHalfOpen:
		cb.lastFailureTime = time.Now()
		cb.transitionTo(CircuitOpen)
	}
}

// State returns the current state.
func (cb *CircuitBreaker) State() CircuitBreakerState {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	return cb.state
}

// transitionTo moves to a new state and resets counters.
// Must be called with cb.mu held.
func (cb *CircuitBreaker) transitionTo(newState CircuitBreakerState) {
	old := cb.state
	cb.state = newState
	cb.failureCount = 0
	cb.successCount = 0
	cb.halfOpenAllowed = 0

	slog.Info("circuit breaker state change",
		"from", old.String(),
		"to", newState.String(),
	)
}
