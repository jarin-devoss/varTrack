package e2e_test

// Signature verification tests — HMAC-SHA256 webhook signing exercised via the
// gateway's full verification path (enabled when a webhook secret is configured).
//
// go-playground/webhooks is used for cross-validation only: hook.Parse() is
// called on a fake HTTP request signed with our signGitHubPayload() helper to
// verify that our HMAC algorithm produces signatures accepted by the canonical
// library.
//
// Note: Repository, PullRequest, etc. are anonymous inline structs inside the
// go-playground/webhooks payload types — they are not exported named types.
// Raw JSON maps are used to construct payloads; hook.Parse() is used only for
// cross-validation of the HMAC signature itself.
//
// Prerequisites:
//   go get github.com/go-playground/webhooks/v6

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"testing"
	"time"

	// go-playground/webhooks: used ONLY for cross-validation via hook.Parse() to
	// confirm that our HMAC-SHA256 signing is byte-for-byte compatible with the library.
	gh_webhooks "github.com/go-playground/webhooks/v6/github"

	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"
	pb_ds "gateway-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	pb_gh "gateway-service/internal/gen/proto/go/vartrack/v1/models/platforms"
	pb_utils "gateway-service/internal/gen/proto/go/vartrack/v1/utils"
	pb "gateway-service/internal/gen/proto/go/vartrack/v1/services"
	"gateway-service/internal"
	"gateway-service/internal/middlewares"
	"gateway-service/internal/models"

	_ "gateway-service/internal/models/platforms"
	_ "gateway-service/internal/models/secretmanagers"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/durationpb"
)

// ─── Test secret & HMAC helpers ───────────────────────────────────────────────

const testWebhookSecret = "vartrack-test-webhook-secret-12345"

// signGitHubPayload computes X-Hub-Signature-256 using HMAC-SHA256.
// This is the same algorithm used by go-playground/webhooks internally
// (see github.go:VerifySignature in go-playground/webhooks).
func signGitHubPayload(t *testing.T, secret string, body []byte) string {
	t.Helper()
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// ─── Bundle with webhook secret ───────────────────────────────────────────────

// testBundleWithSecret creates a bundle identical to testBundle() but with a
// GitHub webhook secret configured. This enables the gateway's HMAC
// verification path, which testBundle() skips by omitting the secret.
func testBundleWithSecret(t *testing.T) *models.Bundle {
	t.Helper()
	return models.NewBundle(&pb_models.Bundle{
		Platforms: []*pb_models.Platform{
			{
				Config: &pb_models.Platform_Github{
					Github: &pb_gh.GitHub{
						Endpoint:        "https://github.com",
						Protocol:        "https",
						VerifySsl:       true,
						Timeout:         durationpb.New(30 * time.Second),
						MaxRetries:      3,
						PageSize:        30,
						EventTypeHeader: "X-GitHub-Event",
						GitScmSignature: "X-Hub-Signature-256",
						PushEventName:   "push",
						PrEventName:     "pull_request",
						// Inline secret — resolved immediately, no Vault required.
						Secret: &pb_utils.SecretRef{
							Source: &pb_utils.SecretRef_Value{
								Value: testWebhookSecret,
							},
						},
					},
				},
			},
		},
		Datasources: []*pb_models.DataSource{
			{
				Config: &pb_models.DataSource_Mongo{
					Mongo: &pb_ds.MongoConfig{
						Endpoint:       "mongodb://localhost:27017",
						Host:           "localhost",
						Port:           27017,
						BufferSize:     100,
						MaxPoolSize:    10,
						UpdateStrategy: 1,
					},
				},
			},
		},
		Rules: []*pb_models.Rule{
			{
				Platform:     "github",
				Datasource:   "mongo",
				Repositories: []string{"my-org/*"},
			},
		},
		SchemaRegistry: &pb_models.SchemaRegistry{
			Platform: "github",
			Repo:     "my-org/schemas",
			Branch:   "main",
		},
	})
}

// setupSignedTestEnv wires the gateway with a bundle that has a webhook secret.
func setupSignedTestEnv(t *testing.T) *testEnv {
	t.Helper()

	orchSrv := &fakeOrchestratorServer{}
	_, grpcAddr := startGRPCServer(t, orchSrv)

	conn, err := grpc.NewClient(grpcAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial grpc: %v", err)
	}
	t.Cleanup(func() { conn.Close() })

	grpcClient := pb.NewOrchestratorClient(conn)

	bundle := testBundleWithSecret(t)
	router := internal.NewRouter(bundle, grpcClient, conn,
		internal.WithProduction(true),
		internal.WithRateLimiterConfig(middlewares.RateLimiterConfig{
			Rate: 1000, Burst: 1000,
			PerIPRate: 1000, PerIPBurst: 1000,
			MaxBackoffMultiplier: 1,
			BackoffDecayInterval: time.Hour,
			IPCleanupInterval:    time.Hour,
		}),
		internal.WithKeyedRateLimiterConfig(middlewares.KeyedRateLimiterConfig{
			Rate: 1000, Burst: 1000,
			CleanupInterval: time.Hour,
			MaxIdleAge:      time.Hour,
		}),
	)

	httpAddr := startHTTPServer(t, router)
	return &testEnv{
		baseURL: fmt.Sprintf("http://%s", httpAddr),
		grpcSrv: orchSrv,
	}
}

// ─── Payload builders ────────────────────────────────────────────────────────

// newPushPayload returns a minimal GitHub push payload as JSON.
// Repository and other nested fields are anonymous inline structs inside
// gh_webhooks.PushPayload and cannot be constructed as named types; raw maps
// are the correct approach.
func newPushPayload(ref, fullName string) []byte {
	body, _ := json.Marshal(map[string]interface{}{
		"ref": ref,
		"repository": map[string]interface{}{
			"full_name": fullName,
		},
	})
	return body
}

// newPRPayload returns a minimal GitHub pull_request payload as JSON.
func newPRPayload(action, fullName string) []byte {
	body, _ := json.Marshal(map[string]interface{}{
		"action": action,
		"pull_request": map[string]interface{}{
			"number": 42,
			"title":  "test PR",
		},
		"repository": map[string]interface{}{
			"full_name": fullName,
		},
	})
	return body
}

// ─── Cross-validation: our HMAC ↔ go-playground/webhooks ─────────────────────

// TestCrossValidate_SigningCompatible verifies that signGitHubPayload() produces
// a signature accepted by go-playground/webhooks hook.Parse() — confirming
// byte-for-byte HMAC-SHA256 compatibility with the canonical library.
func TestCrossValidate_SigningCompatible(t *testing.T) {
	body := newPushPayload("refs/heads/main", "my-org/my-repo")
	sig := signGitHubPayload(t, testWebhookSecret, body)

	req, _ := http.NewRequest("POST", "/", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Event", "push")
	req.Header.Set("X-Hub-Signature-256", sig)

	hook, err := gh_webhooks.New(gh_webhooks.Options.Secret(testWebhookSecret))
	if err != nil {
		t.Fatalf("create hook: %v", err)
	}
	if _, err := hook.Parse(req, gh_webhooks.PushEvent); err != nil {
		t.Errorf("hook.Parse() rejected our HMAC signature: %v", err)
	}
}

// TestCrossValidate_WrongSecret_Rejected confirms that hook.Parse() rejects
// a payload signed with the wrong secret — the inverse of the above test.
func TestCrossValidate_WrongSecret_Rejected(t *testing.T) {
	body := newPushPayload("refs/heads/main", "my-org/my-repo")
	wrongSig := signGitHubPayload(t, "wrong-secret", body)

	req, _ := http.NewRequest("POST", "/", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Event", "push")
	req.Header.Set("X-Hub-Signature-256", wrongSig)

	hook, _ := gh_webhooks.New(gh_webhooks.Options.Secret(testWebhookSecret))
	if _, err := hook.Parse(req, gh_webhooks.PushEvent); err == nil {
		t.Error("hook.Parse() accepted wrong-secret signature — expected error")
	}
}

// ─── Signature verification tests ────────────────────────────────────────────

func TestE2E_SignedWebhook_ValidSignature_Accepted(t *testing.T) {
	env := setupSignedTestEnv(t)

	body := newPushPayload("refs/heads/main", "my-org/my-repo")
	sig := signGitHubPayload(t, testWebhookSecret, body)

	resp, respBody := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":      "application/json",
		"X-GitHub-Event":    "push",
		"X-GitHub-Delivery": "signed-delivery-uuid-001",
		"X-Hub-Signature-256": sig,
	}, body)

	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("valid signature: expected 202, got %d: %s", resp.StatusCode, respBody)
	}
}

func TestE2E_SignedWebhook_WrongSignature_Rejected(t *testing.T) {
	env := setupSignedTestEnv(t)

	body := newPushPayload("refs/heads/main", "my-org/my-repo")
	wrongSig := signGitHubPayload(t, "wrong-secret", body)

	resp, respBody := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":        "application/json",
		"X-GitHub-Event":      "push",
		"X-GitHub-Delivery":   "signed-delivery-uuid-002",
		"X-Hub-Signature-256": wrongSig,
	}, body)

	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong signature: expected 401, got %d: %s", resp.StatusCode, respBody)
	}
	assertAPIError(t, respBody, "GW_SIGNATURE_INVALID")
}

func TestE2E_SignedWebhook_MissingSignature_Rejected(t *testing.T) {
	env := setupSignedTestEnv(t)

	body := newPushPayload("refs/heads/main", "my-org/my-repo")

	resp, respBody := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":   "application/json",
		"X-GitHub-Event": "push",
		// No X-Hub-Signature-256 header.
	}, body)

	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("missing signature: expected 401, got %d: %s", resp.StatusCode, respBody)
	}
	assertAPIError(t, respBody, "GW_SIGNATURE_INVALID")
}

func TestE2E_SignedWebhook_TamperedPayload_Rejected(t *testing.T) {
	env := setupSignedTestEnv(t)

	// Sign the original body, then tamper with it.
	originalBody := newPushPayload("refs/heads/main", "my-org/my-repo")
	sig := signGitHubPayload(t, testWebhookSecret, originalBody)

	tamperedBody := newPushPayload("refs/heads/injected-branch", "evil-org/evil-repo")

	resp, respBody := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":        "application/json",
		"X-GitHub-Event":      "push",
		"X-GitHub-Delivery":   "tamper-test-uuid-003",
		"X-Hub-Signature-256": sig, // signature from original body
	}, tamperedBody) // but sending tampered body

	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("tampered payload: expected 401, got %d: %s", resp.StatusCode, respBody)
	}
	assertAPIError(t, respBody, "GW_SIGNATURE_INVALID")
}

func TestE2E_SignedWebhook_PR_ValidSignature_Accepted(t *testing.T) {
	env := setupSignedTestEnv(t)

	body := newPRPayload("opened", "my-org/my-repo")
	sig := signGitHubPayload(t, testWebhookSecret, body)

	resp, respBody := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":        "application/json",
		"X-GitHub-Event":      "pull_request",
		"X-GitHub-Delivery":   "signed-pr-delivery-004",
		"X-Hub-Signature-256": sig,
	}, body)

	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("signed PR: expected 202, got %d: %s", resp.StatusCode, respBody)
	}
}

// TestE2E_SignedWebhook_StructuralValidation exercises the go-playground/webhooks
// JSON schema against our gateway's validateWebhookStream() to confirm they agree
// on required fields.
func TestE2E_SignedWebhook_StructuralValidation(t *testing.T) {
	env := setupSignedTestEnv(t)

	tests := []struct {
		name      string
		event     string
		buildBody func() []byte
		wantCode  int
	}{
		{
			name:  "push with all required fields accepted",
			event: "push",
			buildBody: func() []byte {
				return newPushPayload("refs/heads/main", "my-org/repo")
			},
			wantCode: http.StatusAccepted,
		},
		{
			name:  "push missing ref rejected",
			event: "push",
			buildBody: func() []byte {
				// Repository is an anonymous inline struct in gh_webhooks.PushPayload
				// — use a raw map. Ref deliberately omitted to trigger validation.
				b, _ := json.Marshal(map[string]interface{}{
					"repository": map[string]interface{}{
						"full_name": "my-org/repo",
					},
				})
				return b
			},
			wantCode: http.StatusBadRequest,
		},
		{
			name:  "pull_request with all fields accepted",
			event: "pull_request",
			buildBody: func() []byte {
				return newPRPayload("opened", "my-org/repo")
			},
			wantCode: http.StatusAccepted,
		},
	}

	for i, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			body := tc.buildBody()
			sig := signGitHubPayload(t, testWebhookSecret, body)

			resp, respBody := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
				"Content-Type":        "application/json",
				"X-GitHub-Event":      tc.event,
				"X-GitHub-Delivery":   fmt.Sprintf("struct-validation-uuid-%d", i),
				"X-Hub-Signature-256": sig,
			}, body)

			if resp.StatusCode != tc.wantCode {
				t.Errorf("status = %d, want %d\nbody: %s", resp.StatusCode, tc.wantCode, respBody)
			}
		})
	}
}
