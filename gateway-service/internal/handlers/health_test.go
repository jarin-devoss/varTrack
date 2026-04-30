package handlers_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"gateway-service/internal/handlers"

	"google.golang.org/grpc/connectivity"
)

// ─── Test doubles ─────────────────────────────────────────────────────────────

type mockConn struct{ state connectivity.State }

func (m *mockConn) GetState() connectivity.State { return m.state }

type mockSMPinger struct{ err error }

func (m *mockSMPinger) Ping(_ context.Context) error { return m.err }

// ─── Helpers ──────────────────────────────────────────────────────────────────

// newHealthHandler builds a HealthHandler backed by the given gRPC state.
func newHealthHandler(t *testing.T, state connectivity.State) *handlers.HealthHandler {
	t.Helper()
	return handlers.NewHealthHandler(&mockConn{state}, nil)
}

type healthBody struct {
	Status string `json:"status"`
	Detail string `json:"detail,omitempty"`
}

// decodeHealthBody deserialises the JSON response body into healthBody.
func decodeHealthBody(t *testing.T, raw []byte) healthBody {
	t.Helper()
	var b healthBody
	if err := json.Unmarshal(raw, &b); err != nil {
		t.Fatalf("decode health response: %v\nbody: %s", err, raw)
	}
	return b
}

// callReadiness issues a readiness probe against the handler and returns the recorder.
func callReadiness(t *testing.T, h *handlers.HealthHandler) *httptest.ResponseRecorder {
	t.Helper()
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/health/readiness", nil)
	h.Readiness(rr, req)
	return rr
}

// ─── Liveness ─────────────────────────────────────────────────────────────────

func TestLiveness(t *testing.T) {
	tests := []struct {
		name        string
		setupFn     func(*handlers.HealthHandler)
		wantStatus  int
		wantBody    string
	}{
		{
			name:       "always 200 when alive",
			setupFn:    func(*handlers.HealthHandler) {},
			wantStatus: http.StatusOK,
			wantBody:   "OK",
		},
		{
			name: "still 200 when shutting down (liveness ignores state)",
			setupFn: func(h *handlers.HealthHandler) {
				h.SetUnavailable()
			},
			wantStatus: http.StatusOK,
			wantBody:   "OK",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			h := newHealthHandler(t, connectivity.Ready)
			tc.setupFn(h)

			rr := httptest.NewRecorder()
			h.Liveness(rr, httptest.NewRequest("GET", "/health/liveness", nil))

			if rr.Code != tc.wantStatus {
				t.Errorf("status = %d, want %d", rr.Code, tc.wantStatus)
			}
			if tc.wantBody != "" && rr.Body.String() != tc.wantBody {
				t.Errorf("body = %q, want %q", rr.Body.String(), tc.wantBody)
			}
		})
	}
}

// ─── Readiness — gRPC connectivity states ─────────────────────────────────────

func TestReadiness_GRPCState(t *testing.T) {
	tests := []struct {
		name       string
		grpcState  connectivity.State
		wantStatus int
		wantReady  bool
	}{
		{"ready", connectivity.Ready, http.StatusOK, true},
		{"idle (acceptable)", connectivity.Idle, http.StatusOK, true},
		{"connecting (transient)", connectivity.Connecting, http.StatusOK, true},
		{"transient_failure", connectivity.TransientFailure, http.StatusServiceUnavailable, false},
		{"shutdown", connectivity.Shutdown, http.StatusServiceUnavailable, false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			h := newHealthHandler(t, tc.grpcState)
			rr := callReadiness(t, h)

			if rr.Code != tc.wantStatus {
				t.Errorf("status = %d, want %d", rr.Code, tc.wantStatus)
			}

			body := decodeHealthBody(t, rr.Body.Bytes())
			if tc.wantReady && body.Status != "READY" {
				t.Errorf("status = %q, want READY", body.Status)
			}
			if !tc.wantReady && body.Status != "NOT_READY" {
				t.Errorf("status = %q, want NOT_READY", body.Status)
			}
		})
	}
}

// ─── Readiness — shutdown / availability flag ─────────────────────────────────

func TestReadiness_Availability(t *testing.T) {
	tests := []struct {
		name       string
		setupFn    func(*handlers.HealthHandler)
		wantStatus int
	}{
		{
			name:       "available and ready",
			setupFn:    func(*handlers.HealthHandler) {},
			wantStatus: http.StatusOK,
		},
		{
			name: "SetUnavailable returns 503",
			setupFn: func(h *handlers.HealthHandler) {
				h.SetUnavailable()
			},
			wantStatus: http.StatusServiceUnavailable,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			h := newHealthHandler(t, connectivity.Ready)
			tc.setupFn(h)
			rr := callReadiness(t, h)
			if rr.Code != tc.wantStatus {
				t.Errorf("status = %d, want %d", rr.Code, tc.wantStatus)
			}
		})
	}
}

func TestReadiness_NilConn_Returns503(t *testing.T) {
	h := handlers.NewHealthHandler(nil, nil)
	rr := callReadiness(t, h)
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("nil conn: status = %d, want 503", rr.Code)
	}
}

// ─── Readiness — secret manager pings ────────────────────────────────────────

func TestReadiness_SecretManagers(t *testing.T) {
	tests := []struct {
		name       string
		managers   map[string]error // name → ping error (nil = healthy)
		wantStatus int
	}{
		{
			name:       "no secret managers — passes",
			managers:   nil,
			wantStatus: http.StatusOK,
		},
		{
			name:       "all healthy",
			managers:   map[string]error{"vault-primary": nil, "vault-secondary": nil},
			wantStatus: http.StatusOK,
		},
		{
			name:       "one unhealthy returns 503",
			managers:   map[string]error{"vault-ok": nil, "vault-bad": errors.New("sealed")},
			wantStatus: http.StatusServiceUnavailable,
		},
		{
			name:       "all unhealthy returns 503",
			managers:   map[string]error{"vault-a": errors.New("connection refused"), "vault-b": errors.New("timeout")},
			wantStatus: http.StatusServiceUnavailable,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			h := newHealthHandler(t, connectivity.Ready)
			for name, err := range tc.managers {
				h.RegisterSecretManager(name, &mockSMPinger{err: err})
			}
			rr := callReadiness(t, h)
			if rr.Code != tc.wantStatus {
				t.Errorf("status = %d, want %d\nbody: %s", rr.Code, tc.wantStatus, rr.Body.String())
			}
		})
	}
}

// ─── Readiness — response format ─────────────────────────────────────────────

func TestReadiness_ResponseFormat(t *testing.T) {
	// Validates the JSON contract that clients depend on.
	h := newHealthHandler(t, connectivity.Ready)
	rr := callReadiness(t, h)

	ct := rr.Header().Get("Content-Type")
	if ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}

	body := decodeHealthBody(t, rr.Body.Bytes())
	if body.Status == "" {
		t.Error("response must contain non-empty 'status' field")
	}
}

func TestReadiness_NotReady_HasDetail(t *testing.T) {
	h := handlers.NewHealthHandler(&mockConn{connectivity.TransientFailure}, nil)
	rr := callReadiness(t, h)

	body := decodeHealthBody(t, rr.Body.Bytes())
	if body.Detail == "" {
		t.Error("NOT_READY response must include a 'detail' field explaining the failure")
	}
}
