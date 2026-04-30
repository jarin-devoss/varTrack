// Package handlers implements HTTP request handlers for webhooks and health checks.
package handlers

import (
	"encoding/json"
	"log/slog"
	"net/http"
)

// ErrorCode is a machine-readable string that identifies the specific
// failure mode. Clients can branch on these codes without parsing the
// human-readable message string.
//
// Convention: SCREAMING_SNAKE_CASE with "GW_" prefix.
type ErrorCode string

const (
	// Request validation errors (4xx).
	ErrCodeInvalidContentType          ErrorCode = "GW_INVALID_CONTENT_TYPE"
	ErrCodeMissingDatasource           ErrorCode = "GW_MISSING_DATASOURCE"
	ErrCodeDatasourceNotFound          ErrorCode = "GW_DATASOURCE_NOT_FOUND"
	ErrCodePlatformMismatch            ErrorCode = "GW_PLATFORM_MISMATCH"
	ErrCodeBodyTooLarge                ErrorCode = "GW_BODY_TOO_LARGE"
	ErrCodeSignatureInvalid            ErrorCode = "GW_SIGNATURE_INVALID"
	ErrCodeReplayDetected              ErrorCode = "GW_REPLAY_DETECTED"
	ErrCodeInvalidJSON                 ErrorCode = "GW_INVALID_JSON"
	ErrCodePayloadValidation           ErrorCode = "GW_PAYLOAD_VALIDATION"
	ErrCodeSchemaRegistryNotConfigured ErrorCode = "GW_SCHEMA_REGISTRY_NOT_CONFIGURED"
	ErrCodePlatformResolutionFailed    ErrorCode = "GW_PLATFORM_RESOLUTION_FAILED"

	// Backend/upstream errors (5xx).
	ErrCodeOrchestratorUnavailable ErrorCode = "GW_ORCHESTRATOR_UNAVAILABLE"
	ErrCodeOrchestratorError       ErrorCode = "GW_ORCHESTRATOR_ERROR"
	ErrCodeBodyReadFailed          ErrorCode = "GW_BODY_READ_FAILED"
)

// apiError is the standard JSON error body for all gateway error responses.
//
// Example:
//
//	{
//	  "code":    "GW_SIGNATURE_INVALID",
//	  "message": "invalid signature",
//	  "status":  401
//	}
type apiError struct {
	Code    ErrorCode `json:"code"`
	Message string    `json:"message"`
	Status  int       `json:"status"`
}

// writeErrorJSON writes a structured JSON error response.
func writeErrorJSON(w http.ResponseWriter, statusCode int, code ErrorCode, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	if err := json.NewEncoder(w).Encode(apiError{
		Code:    code,
		Message: message,
		Status:  statusCode,
	}); err != nil {
		slog.Error("failed to encode error response", "error", err)
	}
}
