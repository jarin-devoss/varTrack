//go:build integration

// Package e2e_test — testcontainers-based integration tests.
//
// These tests require Docker and run only when explicitly requested:
//
//	go test -tags integration ./e2e/...
//
// Each container is wrapped in a GenericContainerRequest with WaitingFor
// to block until the service is actually ready (not just started).
// t.Cleanup() terminates containers even if the test panics.
// Containers expose ephemeral ports so tests never conflict with each other.
//
// Prerequisites:
//
//	go get github.com/testcontainers/testcontainers-go
package e2e_test

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	// testcontainers-go: used here for Vault to exercise the health
	// handler's secret manager ping with a REAL Vault process.
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/wait"

	"gateway-service/internal/handlers"
	"google.golang.org/grpc/connectivity"
)

// ─── Test doubles (scoped to integration build tag) ──────────────────────────

// intGRPCConn satisfies handlers.GRPCConnChecker with a fixed state.
type intGRPCConn struct{ state connectivity.State }

func (c *intGRPCConn) GetState() connectivity.State { return c.state }

// ─── Vault container helper ───────────────────────────────────────────────────

const (
	vaultDevToken = "root"
	vaultDevPort  = "8200/tcp"
)

// startVaultContainer starts a Vault dev-mode container and returns its HTTP
// base URL. WaitingFor blocks until /v1/sys/health returns 200, t.Cleanup
// terminates on test completion.
func startVaultContainer(t *testing.T) string {
	t.Helper()

	ctx := context.Background()
	req := testcontainers.ContainerRequest{
		Image:        "hashicorp/vault:1.17",
		ExposedPorts: []string{vaultDevPort},
		Env: map[string]string{
			"VAULT_DEV_ROOT_TOKEN_ID":  vaultDevToken,
			"VAULT_DEV_LISTEN_ADDRESS": "0.0.0.0:8200",
		},
		Cmd: []string{"server", "-dev"},
		WaitingFor: wait.ForHTTP("/v1/sys/health").
			WithPort(vaultDevPort).
			WithStatusCodeMatcher(func(status int) bool {
				return status == http.StatusOK
			}).
			WithStartupTimeout(60 * time.Second),
	}

	c, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{
		ContainerRequest: req,
		Started:          true,
	})
	if err != nil {
		t.Fatalf("start vault container: %v", err)
	}
	t.Cleanup(func() {
		if err := c.Terminate(context.Background()); err != nil {
			t.Logf("terminate vault container: %v", err)
		}
	})

	host, err := c.Host(ctx)
	if err != nil {
		t.Fatalf("vault container host: %v", err)
	}
	port, err := c.MappedPort(ctx, vaultDevPort)
	if err != nil {
		t.Fatalf("vault container port: %v", err)
	}
	return fmt.Sprintf("http://%s:%s", host, port.Port())
}

// ─── vaultHTTPPinger ─────────────────────────────────────────────────────────

// vaultHTTPPinger implements handlers.SecretManagerPinger against a real Vault
// /v1/sys/health endpoint. No Vault token is needed in dev mode.
type vaultHTTPPinger struct {
	addr   string
	client *http.Client
}

func newVaultPinger(addr string) *vaultHTTPPinger {
	return &vaultHTTPPinger{
		addr:   addr,
		client: &http.Client{Timeout: 3 * time.Second},
	}
}

func (v *vaultHTTPPinger) Ping(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, "GET", v.addr+"/v1/sys/health", nil)
	if err != nil {
		return fmt.Errorf("build vault health request: %w", err)
	}
	resp, err := v.client.Do(req)
	if err != nil {
		return fmt.Errorf("vault health check: %w", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("vault health returned %d", resp.StatusCode)
	}
	return nil
}

var _ handlers.SecretManagerPinger = (*vaultHTTPPinger)(nil)

// ─── Helper to call readiness ─────────────────────────────────────────────────

func callReadinessHandler(t *testing.T, h *handlers.HealthHandler) *httptest.ResponseRecorder {
	t.Helper()
	rr := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/health/readiness", nil)
	h.Readiness(rr, req)
	return rr
}

// ─── Integration tests ────────────────────────────────────────────────────────

// TestIntegration_HealthReadiness_VaultHealthy starts a REAL Vault dev server
// and verifies that the gateway readiness probe returns 200 when the secret
// manager is reachable.
func TestIntegration_HealthReadiness_VaultHealthy(t *testing.T) {
	vaultAddr := startVaultContainer(t)

	h := handlers.NewHealthHandler(&intGRPCConn{connectivity.Ready}, nil)
	h.RegisterSecretManager("vault", newVaultPinger(vaultAddr))

	rr := callReadinessHandler(t, h)
	if rr.Code != http.StatusOK {
		t.Errorf("expected 200 with healthy Vault, got %d: %s", rr.Code, rr.Body.String())
	}
}

// TestIntegration_HealthReadiness_VaultUnreachable verifies that readiness
// returns 503 when the Vault address is unreachable — simulating a secret
// manager going offline at runtime (e.g. Vault pod eviction).
func TestIntegration_HealthReadiness_VaultUnreachable(t *testing.T) {
	// Point to a port that nothing is listening on — immediate connection refusal.
	unreachable := "http://127.0.0.1:19999"

	h := handlers.NewHealthHandler(&intGRPCConn{connectivity.Ready}, nil)
	h.RegisterSecretManager("vault", newVaultPinger(unreachable))

	rr := callReadinessHandler(t, h)
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503 when Vault unreachable, got %d", rr.Code)
	}
}

// TestIntegration_HealthReadiness_VaultTerminated verifies that readiness fails
// after a previously healthy Vault container is stopped.
func TestIntegration_HealthReadiness_VaultTerminated(t *testing.T) {
	ctx := context.Background()

	req := testcontainers.ContainerRequest{
		Image:        "hashicorp/vault:1.17",
		ExposedPorts: []string{vaultDevPort},
		Env: map[string]string{
			"VAULT_DEV_ROOT_TOKEN_ID":  vaultDevToken,
			"VAULT_DEV_LISTEN_ADDRESS": "0.0.0.0:8200",
		},
		Cmd:        []string{"server", "-dev"},
		WaitingFor: wait.ForHTTP("/v1/sys/health").WithPort(vaultDevPort).WithStartupTimeout(60 * time.Second),
	}

	c, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{
		ContainerRequest: req,
		Started:          true,
	})
	if err != nil {
		t.Fatalf("start vault container: %v", err)
	}
	t.Cleanup(func() { c.Terminate(context.Background()) })

	host, _ := c.Host(ctx)
	port, _ := c.MappedPort(ctx, vaultDevPort)
	vaultAddr := fmt.Sprintf("http://%s:%s", host, port.Port())

	pinger := newVaultPinger(vaultAddr)

	// Confirm healthy before termination.
	if err := pinger.Ping(ctx); err != nil {
		t.Fatalf("vault not healthy before termination: %v", err)
	}

	// Stop the container — simulates Vault going down.
	if err := c.Stop(ctx, nil); err != nil {
		t.Fatalf("stop vault: %v", err)
	}

	h := handlers.NewHealthHandler(&intGRPCConn{connectivity.Ready}, nil)
	h.RegisterSecretManager("vault", pinger)

	rr := callReadinessHandler(t, h)
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503 after Vault stopped, got %d: %s", rr.Code, rr.Body.String())
	}
}

// TestIntegration_HealthReadiness_OneOfTwoVaultsFails tests the concurrent SM
// ping — if any single SM is unreachable, readiness must fail.
func TestIntegration_HealthReadiness_OneOfTwoVaultsFails(t *testing.T) {
	// One real Vault + one that's definitely unreachable.
	vaultAddr := startVaultContainer(t)

	h := handlers.NewHealthHandler(&intGRPCConn{connectivity.Ready}, nil)
	h.RegisterSecretManager("vault-ok", newVaultPinger(vaultAddr))
	h.RegisterSecretManager("vault-bad", newVaultPinger("http://127.0.0.1:19998"))

	rr := callReadinessHandler(t, h)
	if rr.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503 when one SM is unreachable, got %d", rr.Code)
	}
}
