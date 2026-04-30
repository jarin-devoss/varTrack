package e2e_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"testing"
	"time"

	"gateway-service/internal"
	"gateway-service/internal/handlers"
	"gateway-service/internal/middlewares"
	"gateway-service/internal/models"

	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"
	pb_ds "gateway-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	pb_gh "gateway-service/internal/gen/proto/go/vartrack/v1/models/platforms"
	pb "gateway-service/internal/gen/proto/go/vartrack/v1/services"

	// Register drivers.
	_ "gateway-service/internal/models/platforms"
	_ "gateway-service/internal/models/secretmanagers"

	"google.golang.org/grpc"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/durationpb"
)

// ───────────────────────────── Test Doubles ──────────────────────────────────

// fakeOrchestratorServer records calls and returns canned responses.
type fakeOrchestratorServer struct {
	pb.UnimplementedOrchestratorServer

	webhookCalls       []*pb.ProcessWebhookRequest
	schemaWebhookCalls []*pb.ProcessSchemaWebhookRequest
}

func (f *fakeOrchestratorServer) ProcessWebhook(_ context.Context, req *pb.ProcessWebhookRequest) (*pb.ProcessWebhookResponse, error) {
	f.webhookCalls = append(f.webhookCalls, req)
	return &pb.ProcessWebhookResponse{TaskId: "task-123", Message: "accepted"}, nil
}

func (f *fakeOrchestratorServer) ProcessSchemaWebhook(_ context.Context, req *pb.ProcessSchemaWebhookRequest) (*pb.ProcessSchemaWebhookResponse, error) {
	f.schemaWebhookCalls = append(f.schemaWebhookCalls, req)
	return &pb.ProcessSchemaWebhookResponse{TaskId: "schema-456", Message: "schema accepted"}, nil
}

// fakeGRPCConn satisfies handlers.GRPCConnChecker.
type fakeGRPCConn struct{}

func (f *fakeGRPCConn) GetState() connectivity.State {
	return connectivity.Ready
}

var _ handlers.GRPCConnChecker = (*fakeGRPCConn)(nil)

// ───────────────────────────── Helpers ───────────────────────────────────────

// testBundle wires a minimal GitHub → mongo bundle with no webhook secret
// so signature verification is skipped in tests.
func testBundle() *models.Bundle {
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
						UpdateStrategy: 1, // STRATEGY_KEY_VALUE
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

// startGRPCServer starts a fake gRPC orchestrator on a random port.
func startGRPCServer(t *testing.T, srv *fakeOrchestratorServer) (*grpc.Server, string) {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("failed to listen: %v", err)
	}

	s := grpc.NewServer()
	pb.RegisterOrchestratorServer(s, srv)

	go func() {
		if err := s.Serve(lis); err != nil {
			// Ignore errors from graceful stop.
		}
	}()

	t.Cleanup(func() { s.GracefulStop() })
	return s, lis.Addr().String()
}

// startHTTPServer starts the gateway HTTP server on a random port.
func startHTTPServer(t *testing.T, handler http.Handler) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("failed to listen: %v", err)
	}

	srv := &http.Server{
		Handler:           handler,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      5 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		if err := srv.Serve(lis); err != nil && err != http.ErrServerClosed {
			// Ignore.
		}
	}()

	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
	})

	return lis.Addr().String()
}

// apiError matches the JSON error response shape from handlers.
type apiError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Status  int    `json:"status"`
}

func doRequest(t *testing.T, method, url string, headers map[string]string, body []byte) (*http.Response, []byte) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, method, url, bytes.NewReader(body))
	if err != nil {
		t.Fatalf("create request: %v", err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}

	respBody, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		t.Fatalf("read response body: %v", err)
	}
	return resp, respBody
}

// ───────────────────────────── Setup ─────────────────────────────────────────

type testEnv struct {
	baseURL string
	grpcSrv *fakeOrchestratorServer
}

func setupTestEnv(t *testing.T, extraOpts ...internal.RouterOption) *testEnv {
	t.Helper()

	// 1. Start fake gRPC orchestrator.
	orchSrv := &fakeOrchestratorServer{}
	_, grpcAddr := startGRPCServer(t, orchSrv)

	// 2. Dial the fake orchestrator.
	conn, err := grpc.NewClient(grpcAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial grpc: %v", err)
	}
	t.Cleanup(func() { conn.Close() })

	grpcClient := pb.NewOrchestratorClient(conn)

	// 3. Wire the router with relaxed rate limits for testing.
	bundle := testBundle()
	opts := []internal.RouterOption{
		internal.WithRateLimiterConfig(middlewares.RateLimiterConfig{
			Rate:                 1000,
			Burst:                1000,
			PerIPRate:            1000,
			PerIPBurst:           1000,
			MaxBackoffMultiplier: 1,
			BackoffDecayInterval: time.Hour,
			IPCleanupInterval:    time.Hour,
		}),
		internal.WithKeyedRateLimiterConfig(middlewares.KeyedRateLimiterConfig{
			Rate:            1000,
			Burst:           1000,
			CleanupInterval: time.Hour,
			MaxIdleAge:      time.Hour,
		}),
	}
	opts = append(opts, extraOpts...)
	router := internal.NewRouter(bundle, grpcClient, conn, opts...)

	// 4. Start HTTP server.
	httpAddr := startHTTPServer(t, router)
	baseURL := fmt.Sprintf("http://%s", httpAddr)

	return &testEnv{
		baseURL: baseURL,
		grpcSrv: orchSrv,
	}
}

// ───────────────────────────── Tests ─────────────────────────────────────────

func TestE2E_WebhookPush_Success(t *testing.T) {
	env := setupTestEnv(t)

	payload := `{"ref":"refs/heads/main","repository":{"full_name":"my-org/my-repo"}}`
	resp, body := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":   "application/json",
		"X-GitHub-Event": "push",
	}, []byte(payload))

	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("expected 202, got %d: %s", resp.StatusCode, string(body))
	}

	if len(env.grpcSrv.webhookCalls) != 1 {
		t.Fatalf("expected 1 gRPC call, got %d", len(env.grpcSrv.webhookCalls))
	}

	call := env.grpcSrv.webhookCalls[0]
	if call.Platform != "github" {
		t.Errorf("expected platform 'github', got %q", call.Platform)
	}
	if call.Datasource != "mongo" {
		t.Errorf("expected datasource 'mongo', got %q", call.Datasource)
	}
	if call.RawPayload != payload {
		t.Errorf("payload mismatch:\n  got:  %s\n  want: %s", call.RawPayload, payload)
	}
}

func TestE2E_WebhookPR_Success(t *testing.T) {
	env := setupTestEnv(t)

	payload := `{"action":"opened","pull_request":{"title":"test"},"repository":{"full_name":"my-org/my-repo"}}`
	resp, body := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":   "application/json",
		"X-GitHub-Event": "pull_request",
	}, []byte(payload))

	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("expected 202, got %d: %s", resp.StatusCode, string(body))
	}

	if len(env.grpcSrv.webhookCalls) != 1 {
		t.Fatalf("expected 1 gRPC call, got %d", len(env.grpcSrv.webhookCalls))
	}
}

func TestE2E_WebhookIgnoredEvent(t *testing.T) {
	env := setupTestEnv(t)

	resp, _ := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":   "application/json",
		"X-GitHub-Event": "ping",
	}, []byte(`{"zen":"Keep it logically awesome."}`))

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 for ignored event, got %d", resp.StatusCode)
	}

	// Ignored events should NOT reach the orchestrator.
	if len(env.grpcSrv.webhookCalls) != 0 {
		t.Errorf("expected 0 gRPC calls for ignored event, got %d", len(env.grpcSrv.webhookCalls))
	}
}

func TestE2E_WebhookDatasourceNotFound(t *testing.T) {
	env := setupTestEnv(t)

	resp, body := doRequest(t, "POST", env.baseURL+"/webhooks/nonexistent", map[string]string{
		"Content-Type":   "application/json",
		"X-GitHub-Event": "push",
	}, []byte(`{"ref":"refs/heads/main"}`))

	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404, got %d: %s", resp.StatusCode, string(body))
	}

	var errResp apiError
	if err := json.Unmarshal(body, &errResp); err != nil {
		t.Fatalf("unmarshal error response: %v", err)
	}
	if errResp.Code != "GW_DATASOURCE_NOT_FOUND" {
		t.Errorf("expected code GW_DATASOURCE_NOT_FOUND, got %q", errResp.Code)
	}
}

func TestE2E_WebhookInvalidContentType(t *testing.T) {
	env := setupTestEnv(t)

	resp, body := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":   "text/plain",
		"X-GitHub-Event": "push",
	}, []byte(`hello`))

	if resp.StatusCode != http.StatusUnsupportedMediaType {
		t.Fatalf("expected 415, got %d: %s", resp.StatusCode, string(body))
	}

	var errResp apiError
	if err := json.Unmarshal(body, &errResp); err != nil {
		t.Fatalf("unmarshal error response: %v", err)
	}
	if errResp.Code != "GW_INVALID_CONTENT_TYPE" {
		t.Errorf("expected code GW_INVALID_CONTENT_TYPE, got %q", errResp.Code)
	}
}

func TestE2E_WebhookMissingEventHeader(t *testing.T) {
	env := setupTestEnv(t)

	resp, body := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type": "application/json",
		// No X-GitHub-Event header.
	}, []byte(`{"ref":"refs/heads/main"}`))

	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", resp.StatusCode, string(body))
	}

	var errResp apiError
	if err := json.Unmarshal(body, &errResp); err != nil {
		t.Fatalf("unmarshal error response: %v", err)
	}
	if errResp.Code != "GW_PLATFORM_MISMATCH" {
		t.Errorf("expected code GW_PLATFORM_MISMATCH, got %q", errResp.Code)
	}
}

func TestE2E_WebhookInvalidJSON(t *testing.T) {
	env := setupTestEnv(t)

	resp, body := doRequest(t, "POST", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type":   "application/json",
		"X-GitHub-Event": "push",
	}, []byte(`{broken json`))

	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", resp.StatusCode, string(body))
	}

	var errResp apiError
	if err := json.Unmarshal(body, &errResp); err != nil {
		t.Fatalf("unmarshal error response: %v", err)
	}
	if errResp.Code != "GW_INVALID_JSON" {
		t.Errorf("expected code GW_INVALID_JSON, got %q", errResp.Code)
	}
}

func TestE2E_SchemaRegistryWebhook_Success(t *testing.T) {
	env := setupTestEnv(t)

	payload := `{"ref":"refs/heads/main","repository":{"full_name":"my-org/schemas"}}`
	resp, body := doRequest(t, "POST", env.baseURL+"/webhooks/schema-registry", map[string]string{
		"Content-Type":   "application/json",
		"X-GitHub-Event": "push",
	}, []byte(payload))

	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("expected 202, got %d: %s", resp.StatusCode, string(body))
	}

	if len(env.grpcSrv.schemaWebhookCalls) != 1 {
		t.Fatalf("expected 1 schema gRPC call, got %d", len(env.grpcSrv.schemaWebhookCalls))
	}

	call := env.grpcSrv.schemaWebhookCalls[0]
	if call.Platform != "github" {
		t.Errorf("schema webhook: expected platform 'github', got %q", call.Platform)
	}
	if call.Repo != "my-org/schemas" {
		t.Errorf("schema webhook: expected repo 'my-org/schemas', got %q", call.Repo)
	}
}

func TestE2E_HealthLiveness(t *testing.T) {
	env := setupTestEnv(t)

	resp, body := doRequest(t, "GET", env.baseURL+"/health/liveness", nil, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", resp.StatusCode, string(body))
	}
}

func TestE2E_HealthReadiness(t *testing.T) {
	env := setupTestEnv(t)

	resp, _ := doRequest(t, "GET", env.baseURL+"/health/readiness", nil, nil)
	// The gRPC conn is ready, so readiness should pass.
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}

func TestE2E_SecurityHeaders(t *testing.T) {
	env := setupTestEnv(t)

	resp, _ := doRequest(t, "GET", env.baseURL+"/health/liveness", nil, nil)

	checks := map[string]string{
		"X-Content-Type-Options": "nosniff",
		"X-Frame-Options":        "DENY",
		"Cache-Control":          "no-store, no-cache, must-revalidate",
	}
	for header, want := range checks {
		got := resp.Header.Get(header)
		if got != want {
			t.Errorf("header %q = %q, want %q", header, got, want)
		}
	}
}

func TestE2E_RequestIDHeader(t *testing.T) {
	env := setupTestEnv(t)

	resp, _ := doRequest(t, "GET", env.baseURL+"/health/liveness", nil, nil)

	if resp.Header.Get("X-Request-ID") == "" {
		t.Error("expected X-Request-ID header to be set")
	}
}

func TestE2E_MethodNotAllowed(t *testing.T) {
	env := setupTestEnv(t)

	resp, _ := doRequest(t, "GET", env.baseURL+"/webhooks/mongo", map[string]string{
		"Content-Type": "application/json",
	}, nil)

	// GET on a POST-only endpoint → the mux returns 405.
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", resp.StatusCode)
	}
}
