// Package monitoring – webhook outcome labels keep metric cardinality bounded
// and make dashboards/alerts consistent across the codebase.
package monitoring

// WebhookOutcome is a type-safe label value for gw_webhooks_total{outcome=...}.
// Using a named type prevents mismatches between call-sites.
type WebhookOutcome string

const (
	// OutcomeAccepted means the webhook was forwarded to the orchestrator.
	OutcomeAccepted WebhookOutcome = "accepted"

	// OutcomeIgnored means the event type was not push or PR (e.g. "ping").
	OutcomeIgnored WebhookOutcome = "ignored"

	// OutcomeInvalidContentType means Content-Type was not application/json.
	OutcomeInvalidContentType WebhookOutcome = "invalid_content_type"

	// OutcomeInvalidJSON means the body was not parseable JSON.
	OutcomeInvalidJSON WebhookOutcome = "invalid_json"

	// OutcomeInvalidSignature means the HMAC signature check failed.
	OutcomeInvalidSignature WebhookOutcome = "invalid_signature"

	// OutcomeReplayDetected means the timestamp was too old or in the future.
	OutcomeReplayDetected WebhookOutcome = "replay_detected"

	// OutcomeValidationFailed means the structural payload check failed.
	OutcomeValidationFailed WebhookOutcome = "validation_failed"

	// OutcomeDatasourceNotFound means no rule exists for the datasource.
	OutcomeDatasourceNotFound WebhookOutcome = "datasource_not_found"

	// OutcomePlatformMismatch means the expected event-type header was missing.
	OutcomePlatformMismatch WebhookOutcome = "platform_mismatch"

	// OutcomeOrchestratorError means the gRPC call to the orchestrator failed.
	OutcomeOrchestratorError WebhookOutcome = "orchestrator_error"

	// OutcomeCircuitOpen means the request was fast-failed due to the circuit breaker.
	OutcomeCircuitOpen WebhookOutcome = "circuit_open"
)

// String makes WebhookOutcome implement the fmt.Stringer interface.
func (o WebhookOutcome) String() string { return string(o) }
