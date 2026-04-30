package monitoring_test

import (
	"context"
	"errors"
	"testing"

	"gateway-service/internal/monitoring"
)

// ─── Tracer ───────────────────────────────────────────────────────────────────

func TestSpan_EndNoError(t *testing.T) {
	ctx := context.Background()
	_, span := monitoring.Start(ctx, "test.operation")
	span.End(nil) // must not panic
}

func TestSpan_EndWithError(t *testing.T) {
	ctx := context.Background()
	_, span := monitoring.Start(ctx, "test.error")
	span.End(errors.New("something went wrong")) // must not panic
}

func TestSpan_SetAttr(t *testing.T) {
	ctx := context.Background()
	_, span := monitoring.Start(ctx, "test.attrs")
	span.SetAttr("key", "value")
	span.SetAttr("count", 42)
	span.End(nil)
}

func TestStartWebhookSpan(t *testing.T) {
	ctx := context.Background()
	newCtx, span := monitoring.StartWebhookSpan(ctx, "github", "mongo", "push")
	if newCtx == nil {
		t.Fatal("StartWebhookSpan returned nil context")
	}
	span.End(nil)
}

func TestStartOrchestratorSpan(t *testing.T) {
	ctx := context.Background()
	newCtx, span := monitoring.StartOrchestratorSpan(ctx, "ProcessWebhook")
	if newCtx == nil {
		t.Fatal("StartOrchestratorSpan returned nil context")
	}
	span.End(nil)
}

// ─── Outcomes ─────────────────────────────────────────────────────────────────

func TestWebhookOutcome_String(t *testing.T) {
	tests := []struct {
		outcome monitoring.WebhookOutcome
		want    string
	}{
		{monitoring.OutcomeAccepted, "accepted"},
		{monitoring.OutcomeIgnored, "ignored"},
		{monitoring.OutcomeInvalidContentType, "invalid_content_type"},
		{monitoring.OutcomeInvalidJSON, "invalid_json"},
		{monitoring.OutcomeInvalidSignature, "invalid_signature"},
		{monitoring.OutcomeReplayDetected, "replay_detected"},
		{monitoring.OutcomeValidationFailed, "validation_failed"},
		{monitoring.OutcomeDatasourceNotFound, "datasource_not_found"},
		{monitoring.OutcomePlatformMismatch, "platform_mismatch"},
		{monitoring.OutcomeOrchestratorError, "orchestrator_error"},
		{monitoring.OutcomeCircuitOpen, "circuit_open"},
	}
	for _, tt := range tests {
		if got := tt.outcome.String(); got != tt.want {
			t.Errorf("outcome %q: String() = %q, want %q", tt.outcome, got, tt.want)
		}
	}
}

// ─── GRPCStateToLabel ─────────────────────────────────────────────────────────

type stubStringer struct{ s string }

func (ss stubStringer) String() string { return ss.s }

func TestGRPCStateToLabel(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"IDLE", "idle"},
		{"CONNECTING", "connecting"},
		{"READY", "ready"},
		{"TRANSIENT_FAILURE", "transient_failure"},
		{"SHUTDOWN", "shutdown"},
		{"UNKNOWN_STATE", "unknown"},
	}
	for _, tt := range tests {
		got := monitoring.GRPCStateToLabel(stubStringer{tt.input})
		if got != tt.want {
			t.Errorf("GRPCStateToLabel(%q) = %q, want %q", tt.input, got, tt.want)
		}
	}
}

// ─── DefaultBuildInfo ─────────────────────────────────────────────────────────

func TestDefaultBuildInfo_GoVersionSet(t *testing.T) {
	info := monitoring.DefaultBuildInfo()
	if info.GoVersion == "" {
		t.Error("GoVersion should not be empty")
	}
	if info.Version == "" {
		t.Error("Version should not be empty")
	}
}

// ─── Init smoke test ──────────────────────────────────────────────────────────

func TestInit_NilConn(t *testing.T) {
	ctx := context.Background()
	// Init with nil conn must not panic.
	m := monitoring.Init(ctx, monitoring.DefaultBuildInfo(), nil)
	if m == nil {
		t.Fatal("Init returned nil metrics")
	}
}
