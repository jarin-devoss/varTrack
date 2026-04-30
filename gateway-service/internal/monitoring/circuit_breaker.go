package monitoring

import (
	"log/slog"

	"gateway-service/internal/middlewares"
)

// InstrumentedCircuitBreaker wraps a *middlewares.CircuitBreaker and emits
// metrics on every state change and on every rejection.
//
// It uses the circuit breaker's public API (Allow / RecordSuccess /
// RecordFailure / State) and adds metric calls at each decision point.
//
// Design note: because CircuitBreaker stores state internally and the
// transition happens inside RecordFailure/RecordSuccess, we detect state
// changes by comparing the state before and after each call.
type InstrumentedCircuitBreaker struct {
	inner   *middlewares.CircuitBreaker
	metrics *GatewayMetrics
}

// NewInstrumentedCircuitBreaker wraps cb with metrics.
func NewInstrumentedCircuitBreaker(cb *middlewares.CircuitBreaker, m *GatewayMetrics) *InstrumentedCircuitBreaker {
	icb := &InstrumentedCircuitBreaker{inner: cb, metrics: m}

	// Emit initial state so the gauge is non-zero from startup.
	icb.metrics.SetCircuitBreakerState(float64(cb.State()))
	return icb
}

// Allow delegates to the wrapped breaker and increments the rejection
// counter if the request is not allowed.
func (icb *InstrumentedCircuitBreaker) Allow() bool {
	ok := icb.inner.Allow()
	if !ok {
		icb.metrics.IncCircuitBreakerOpenRejection()
		slog.Debug("circuit breaker: request rejected (open)")
	}
	return ok
}

// RecordSuccess delegates to the wrapped breaker and detects state transitions.
func (icb *InstrumentedCircuitBreaker) RecordSuccess() {
	before := icb.inner.State()
	icb.inner.RecordSuccess()
	after := icb.inner.State()
	icb.onStateChange(before, after)
}

// RecordFailure delegates to the wrapped breaker and detects state transitions.
func (icb *InstrumentedCircuitBreaker) RecordFailure() {
	before := icb.inner.State()
	icb.inner.RecordFailure()
	after := icb.inner.State()
	icb.onStateChange(before, after)
}

// State returns the current circuit breaker state.
func (icb *InstrumentedCircuitBreaker) State() middlewares.CircuitBreakerState {
	return icb.inner.State()
}

// Inner returns the underlying CircuitBreaker (for tests or admin endpoints).
func (icb *InstrumentedCircuitBreaker) Inner() *middlewares.CircuitBreaker {
	return icb.inner
}

func (icb *InstrumentedCircuitBreaker) onStateChange(before, after middlewares.CircuitBreakerState) {
	if before == after {
		return
	}
	fromLabel := before.String()
	toLabel := after.String()

	icb.metrics.IncCircuitBreakerStateChange(fromLabel, toLabel)
	icb.metrics.SetCircuitBreakerState(float64(after))

	slog.Info("circuit breaker state transition recorded in metrics",
		"from", fromLabel,
		"to", toLabel,
	)
}
