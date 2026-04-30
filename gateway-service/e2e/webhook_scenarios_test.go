package e2e_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"testing"

	"gateway-service/internal"
)

// ─── withGateway ──────────────────────────────────────────────────────────────

// withGateway spins up a complete gateway + fake gRPC orchestrator, executes fn,
// then tears everything down via t.Cleanup (not defer, which runs before subtests).
func withGateway(t *testing.T, fn func(baseURL string, grpc *fakeOrchestratorServer)) {
	t.Helper()
	env := setupTestEnv(t) // registers t.Cleanup internally
	fn(env.baseURL, env.grpcSrv)
}

// withProductionGateway is like withGateway but enables production mode so
// that HMAC verification and replay protection are active.
func withProductionGateway(t *testing.T, fn func(baseURL string, grpc *fakeOrchestratorServer)) {
	t.Helper()
	env := setupTestEnv(t, internal.WithProduction(true))
	fn(env.baseURL, env.grpcSrv)
}

// assertAPIError decodes the response body as an apiError and validates the code.
func assertAPIError(t *testing.T, body []byte, wantCode string) {
	t.Helper()
	var e apiError
	if err := json.Unmarshal(body, &e); err != nil {
		t.Fatalf("decode error body: %v\nbody: %s", err, body)
	}
	if string(e.Code) != wantCode {
		t.Errorf("error code = %q, want %q (message: %s)", e.Code, wantCode, e.Message)
	}
}

// ─── Replay Protection ────────────────────────────────────────────────────────

func TestE2E_ReplayAttack_DuplicateDeliveryID_Rejected(t *testing.T) {
	withProductionGateway(t, func(base string, _ *fakeOrchestratorServer) {
		payload := `{"ref":"refs/heads/main","repository":{"full_name":"my-org/my-repo"}}`
		headers := map[string]string{
			"Content-Type":      "application/json",
			"X-GitHub-Event":    "push",
			"X-GitHub-Delivery": "replay-test-uuid-12345",
		}

		resp1, body1 := doRequest(t, "POST", base+"/webhooks/mongo", headers, []byte(payload))
		if resp1.StatusCode != http.StatusAccepted {
			t.Fatalf("first delivery: expected 202, got %d: %s", resp1.StatusCode, body1)
		}

		resp2, body2 := doRequest(t, "POST", base+"/webhooks/mongo", headers, []byte(payload))
		if resp2.StatusCode != http.StatusUnauthorized {
			t.Fatalf("replay: expected 401, got %d: %s", resp2.StatusCode, body2)
		}
		assertAPIError(t, body2, "GW_REPLAY_DETECTED")
	})
}

func TestE2E_DifferentDeliveryIDs_BothAccepted(t *testing.T) {
	withGateway(t, func(base string, _ *fakeOrchestratorServer) {
		payload := `{"ref":"refs/heads/main","repository":{"full_name":"my-org/my-repo"}}`
		for i, id := range []string{"unique-id-aaa", "unique-id-bbb"} {
			resp, body := doRequest(t, "POST", base+"/webhooks/mongo", map[string]string{
				"Content-Type":      "application/json",
				"X-GitHub-Event":    "push",
				"X-GitHub-Delivery": id,
			}, []byte(payload))
			if resp.StatusCode != http.StatusAccepted {
				t.Errorf("delivery %d (%s): expected 202, got %d: %s", i, id, resp.StatusCode, body)
			}
		}
	})
}

// ─── Body Size Limits ─────────────────────────────────────────────────────────

func TestE2E_WebhookBodyTooLarge_Rejected(t *testing.T) {
	withGateway(t, func(base string, _ *fakeOrchestratorServer) {
		// Valid JSON consumed by the structural validator, followed by >10 MB of
		// padding — io.Copy then trips http.MaxBytesReader → 413.
		prefix := []byte(`{"ref":"refs/heads/main","repository":{"full_name":"my-org/repo"}}`)
		padding := make([]byte, (10<<20)+1)
		body := append(prefix, padding...)

		resp, _ := doRequest(t, "POST", base+"/webhooks/mongo", map[string]string{
			"Content-Type":   "application/json",
			"X-GitHub-Event": "push",
		}, body)

		if resp.StatusCode != http.StatusRequestEntityTooLarge {
			t.Errorf("expected 413 for oversized body, got %d", resp.StatusCode)
		}
	})
}

// ─── Correlation ID Propagation ───────────────────────────────────────────────

func TestE2E_CorrelationID(t *testing.T) {
	tests := []struct {
		name       string
		sendCID    string
		wantExact  string // non-empty: must match exactly
		wantMaxLen int    // non-zero: must be ≤ this length
		wantNonEmpty bool
	}{
		{
			name:         "generated when absent",
			sendCID:      "",
			wantNonEmpty: true,
		},
		{
			name:      "preserved from upstream",
			sendCID:   "upstream-trace-id-xyz",
			wantExact: "upstream-trace-id-xyz",
		},
		{
			name:       "200-char ID truncated to 128",
			sendCID:    func() string { s := ""; for range 200 { s += "z" }; return s }(),
			wantMaxLen: 128,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			withGateway(t, func(base string, _ *fakeOrchestratorServer) {
				headers := map[string]string{}
				if tc.sendCID != "" {
					headers["X-Correlation-ID"] = tc.sendCID
				}
				resp, _ := doRequest(t, "GET", base+"/health/liveness", headers, nil)

				got := resp.Header.Get("X-Correlation-ID")
				if tc.wantExact != "" && got != tc.wantExact {
					t.Errorf("X-Correlation-ID = %q, want %q", got, tc.wantExact)
				}
				if tc.wantMaxLen != 0 && len(got) > tc.wantMaxLen {
					t.Errorf("X-Correlation-ID len = %d, want ≤ %d", len(got), tc.wantMaxLen)
				}
				if tc.wantNonEmpty && got == "" {
					t.Error("X-Correlation-ID must be generated when absent")
				}
			})
		})
	}
}

// ─── Payload Validation ───────────────────────────────────────────────────────

func TestE2E_WebhookPayloadValidation(t *testing.T) {
	tests := []struct {
		name      string
		event     string
		payload   string
		wantCode  int
		wantError string
	}{
		{
			name:      "push missing ref",
			event:     "push",
			payload:   `{"repository":{"full_name":"my-org/repo"}}`,
			wantCode:  http.StatusBadRequest,
			wantError: "GW_PAYLOAD_VALIDATION",
		},
		{
			name:      "push missing repository",
			event:     "push",
			payload:   `{"ref":"refs/heads/main"}`,
			wantCode:  http.StatusBadRequest,
			wantError: "GW_PAYLOAD_VALIDATION",
		},
		{
			name:      "pull_request missing action",
			event:     "pull_request",
			payload:   `{"pull_request":{},"repository":{"full_name":"my-org/repo"}}`,
			wantCode:  http.StatusBadRequest,
			wantError: "GW_PAYLOAD_VALIDATION",
		},
		{
			name:      "pull_request missing pull_request field",
			event:     "pull_request",
			payload:   `{"action":"opened","repository":{"full_name":"my-org/repo"}}`,
			wantCode:  http.StatusBadRequest,
			wantError: "GW_PAYLOAD_VALIDATION",
		},
		{
			name:     "valid push accepted",
			event:    "push",
			payload:  `{"ref":"refs/heads/main","repository":{"full_name":"my-org/repo"}}`,
			wantCode: http.StatusAccepted,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			withGateway(t, func(base string, _ *fakeOrchestratorServer) {
				resp, body := doRequest(t, "POST", base+"/webhooks/mongo", map[string]string{
					"Content-Type":      "application/json",
					"X-GitHub-Event":    tc.event,
					"X-GitHub-Delivery": "validation-test-" + tc.name,
				}, []byte(tc.payload))

				if resp.StatusCode != tc.wantCode {
					t.Errorf("status = %d, want %d\nbody: %s", resp.StatusCode, tc.wantCode, body)
				}
				if tc.wantError != "" {
					assertAPIError(t, body, tc.wantError)
				}
			})
		})
	}
}

// ─── Schema Registry ─────────────────────────────────────────────────────────

func TestE2E_SchemaRegistry(t *testing.T) {
	tests := []struct {
		name      string
		event     string
		payload   string
		wantCode  int
		wantError string
	}{
		{
			name:     "push to correct repo accepted",
			event:    "push",
			payload:  `{"ref":"refs/heads/main","repository":{"full_name":"my-org/schemas"}}`,
			wantCode: http.StatusAccepted,
		},
		{
			name:      "push to wrong repo rejected",
			event:     "push",
			payload:   `{"ref":"refs/heads/main","repository":{"full_name":"other-org/other-repo"}}`,
			wantCode:  http.StatusBadRequest,
			wantError: "GW_PAYLOAD_VALIDATION",
		},
		{
			name:     "non-push event ignored",
			event:    "pull_request",
			payload:  `{"action":"opened","pull_request":{},"repository":{"full_name":"my-org/schemas"}}`,
			wantCode: http.StatusOK, // ignored
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			withGateway(t, func(base string, _ *fakeOrchestratorServer) {
				resp, body := doRequest(t, "POST", base+"/webhooks/schema-registry", map[string]string{
					"Content-Type":   "application/json",
					"X-GitHub-Event": tc.event,
				}, []byte(tc.payload))

				if resp.StatusCode != tc.wantCode {
					t.Errorf("status = %d, want %d\nbody: %s", resp.StatusCode, tc.wantCode, body)
				}
				if tc.wantError != "" {
					assertAPIError(t, body, tc.wantError)
				}
			})
		})
	}
}

// ─── HTTP method enforcement ──────────────────────────────────────────────────

func TestE2E_WebhookMethodEnforcement(t *testing.T) {
	tests := []struct {
		method string
	}{
		{"GET"},
		{"PUT"},
		{"DELETE"},
		{"PATCH"},
	}

	for _, tc := range tests {
		t.Run(tc.method, func(t *testing.T) {
			withGateway(t, func(base string, _ *fakeOrchestratorServer) {
				resp, _ := doRequest(t, tc.method, base+"/webhooks/mongo", nil, nil)
				if resp.StatusCode != http.StatusMethodNotAllowed {
					t.Errorf("%s /webhooks/mongo: status = %d, want 405", tc.method, resp.StatusCode)
				}
			})
		})
	}
}

// ─── Security headers — full suite ───────────────────────────────────────────

func TestE2E_SecurityHeaders_FullSuite(t *testing.T) {
	// Validates all five defensive headers, not just three.
	expected := map[string]string{
		"X-Content-Type-Options": "nosniff",
		"X-Frame-Options":        "DENY",
		"Cache-Control":          "no-store, no-cache, must-revalidate",
		"Expires":                "0",
		"Pragma":                 "no-cache",
	}

	withGateway(t, func(base string, _ *fakeOrchestratorServer) {
		resp, _ := doRequest(t, "GET", base+"/health/liveness", nil, nil)
		for header, want := range expected {
			t.Run(header, func(t *testing.T) {
				if got := resp.Header.Get(header); got != want {
					t.Errorf("%q = %q, want %q", header, got, want)
				}
			})
		}
	})
}

// ─── X-Request-ID uniqueness under concurrency ────────────────────────────────

func TestE2E_RequestID_UniqueAcrossConcurrentRequests(t *testing.T) {
	withGateway(t, func(base string, _ *fakeOrchestratorServer) {
		const n = 50
		ids := make([]string, n)
		var wg sync.WaitGroup
		wg.Add(n)

		for i := range n {
			go func(i int) {
				defer wg.Done()
				resp, _ := doRequest(t, "GET", base+"/health/liveness", nil, nil)
				ids[i] = resp.Header.Get("X-Request-ID")
			}(i)
		}
		wg.Wait()

		seen := make(map[string]struct{}, n)
		for i, id := range ids {
			if id == "" {
				t.Errorf("request %d: empty X-Request-ID", i)
				continue
			}
			if _, dup := seen[id]; dup {
				t.Errorf("duplicate X-Request-ID: %q", id)
			}
			seen[id] = struct{}{}
		}
	})
}

// ─── Concurrent webhooks ──────────────────────────────────────────────────────

func TestE2E_ConcurrentWebhooks_AllSucceed(t *testing.T) {
	withGateway(t, func(base string, _ *fakeOrchestratorServer) {
		const n = 20
		results := make([]int, n)
		var wg sync.WaitGroup
		wg.Add(n)

		for i := range n {
			go func(i int) {
				defer wg.Done()
				payload := `{"ref":"refs/heads/main","repository":{"full_name":"my-org/my-repo"}}`
				resp, _ := doRequest(t, "POST", base+"/webhooks/mongo", map[string]string{
					"Content-Type":      "application/json",
					"X-GitHub-Event":    "push",
					"X-GitHub-Delivery": fmt.Sprintf("concurrent-uuid-%d", i),
				}, []byte(payload))
				results[i] = resp.StatusCode
			}(i)
		}
		wg.Wait()

		for i, code := range results {
			if code != http.StatusAccepted {
				t.Errorf("request %d: status = %d, want 202", i, code)
			}
		}
	})
}

// ─── gRPC metadata forwarding ─────────────────────────────────────────────────

func TestE2E_gRPC_ReceivedAt_Set(t *testing.T) {
	withGateway(t, func(base string, grpc *fakeOrchestratorServer) {
		payload := `{"ref":"refs/heads/main","repository":{"full_name":"my-org/my-repo"}}`
		resp, _ := doRequest(t, "POST", base+"/webhooks/mongo", map[string]string{
			"Content-Type":      "application/json",
			"X-GitHub-Event":    "push",
			"X-GitHub-Delivery": "received-at-test-uuid",
		}, []byte(payload))

		if resp.StatusCode != http.StatusAccepted {
			t.Fatalf("expected 202, got %d", resp.StatusCode)
		}
		if len(grpc.webhookCalls) == 0 {
			t.Fatal("expected at least 1 gRPC call")
		}
		call := grpc.webhookCalls[len(grpc.webhookCalls)-1]
		if call.ReceivedAt == nil {
			t.Error("ReceivedAt must be set in the gRPC request")
		}
	})
}

func TestE2E_gRPC_Headers_Forwarded(t *testing.T) {
	withGateway(t, func(base string, grpc *fakeOrchestratorServer) {
		payload := `{"ref":"refs/heads/main","repository":{"full_name":"my-org/my-repo"}}`
		resp, _ := doRequest(t, "POST", base+"/webhooks/mongo", map[string]string{
			"Content-Type":      "application/json",
			"X-GitHub-Event":    "push",
			"X-GitHub-Delivery": "headers-forward-uuid",
		}, []byte(payload))

		if resp.StatusCode != http.StatusAccepted {
			t.Fatalf("expected 202, got %d", resp.StatusCode)
		}
		call := grpc.webhookCalls[len(grpc.webhookCalls)-1]
		if len(call.Headers) == 0 {
			t.Error("request headers must be forwarded in the gRPC request")
		}
	})
}

// ─── Health endpoint response format ─────────────────────────────────────────

func TestE2E_HealthLiveness_Body(t *testing.T) {
	withGateway(t, func(base string, _ *fakeOrchestratorServer) {
		_, body := doRequest(t, "GET", base+"/health/liveness", nil, nil)
		if string(body) != "OK" {
			t.Errorf("liveness body = %q, want \"OK\"", string(body))
		}
	})
}

func TestE2E_HealthReadiness_JSONResponse(t *testing.T) {
	withGateway(t, func(base string, _ *fakeOrchestratorServer) {
		resp, body := doRequest(t, "GET", base+"/health/readiness", nil, nil)

		t.Run("content-type", func(t *testing.T) {
			if ct := resp.Header.Get("Content-Type"); ct != "application/json" {
				t.Errorf("Content-Type = %q, want application/json", ct)
			}
		})
		t.Run("valid-json", func(t *testing.T) {
			var v struct{ Status string `json:"status"` }
			if err := json.Unmarshal(body, &v); err != nil {
				t.Fatalf("readiness body is not valid JSON: %v\nbody: %s", err, body)
			}
			if v.Status == "" {
				t.Error("readiness response must include a non-empty 'status' field")
			}
		})
	})
}
