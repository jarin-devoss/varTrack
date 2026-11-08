package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"gateway-service/internal/degraded"
	pb "vartrack/libs/gen/vartrack/v1/services"

	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc/connectivity"
)

// GRPCConnChecker is the subset of grpc.ClientConn needed by the health handler.
type GRPCConnChecker interface {
	GetState() connectivity.State
}

// SecretManagerPinger verifies connectivity to a secret manager backend.
type SecretManagerPinger interface {
	Ping(ctx context.Context) error
}

// HealthHandler serves liveness and readiness probes.
type HealthHandler struct {
	conn   GRPCConnChecker
	client pb.OrchestratorClient

	secretManagers map[string]SecretManagerPinger
	smMu           sync.RWMutex

	// dependencies are what the gateway needs besides the orchestrator and the secret managers
	// (the nonce store): each is asked on every readiness probe, and one that does not answer
	// makes the gateway not ready.
	dependencies map[string]func(context.Context) error

	available          atomic.Bool
	terminateRequested atomic.Bool
}

// NewHealthHandler creates a new HealthHandler.
func NewHealthHandler(conn GRPCConnChecker, client pb.OrchestratorClient) *HealthHandler {
	h := &HealthHandler{
		conn:           conn,
		client:         client,
		secretManagers: make(map[string]SecretManagerPinger),
		dependencies:   make(map[string]func(context.Context) error),
	}
	h.available.Store(true)
	return h
}

// RegisterSecretManager adds a named secret manager to the readiness check.
func (h *HealthHandler) RegisterSecretManager(name string, sm SecretManagerPinger) {
	h.smMu.Lock()
	defer h.smMu.Unlock()
	h.secretManagers[name] = sm
}

// RegisterDependency adds a named dependency to the readiness check: check returns an error when it
// cannot be reached.
func (h *HealthHandler) RegisterDependency(name string, check func(context.Context) error) {
	h.smMu.Lock()
	defer h.smMu.Unlock()
	h.dependencies[name] = check
}

// SetUnavailable marks the server as shutting down.
func (h *HealthHandler) SetUnavailable() {
	h.terminateRequested.Store(true)
	h.available.Store(false)
}

// Liveness returns 200 as long as the process is alive.
// It deliberately does NOT check external dependencies.
func (h *HealthHandler) Liveness(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("OK"))
}

// Readiness verifies the gateway can serve traffic.
//
// Check order:
//  1. terminateRequested? → 503
//  2. available? → 503
//  3. gRPC orchestrator connection reachable?
//  4. Secret Manager backends reachable?
func (h *HealthHandler) Readiness(w http.ResponseWriter, r *http.Request) {
	start := time.Now()

	if h.terminateRequested.Load() {
		writeHealthJSON(w, http.StatusServiceUnavailable, "NOT_READY",
			"server is terminating and unable to serve requests")
		return
	}
	if !h.available.Load() {
		writeHealthJSON(w, http.StatusServiceUnavailable, "NOT_READY",
			"server is not available: it either hasn't started or is restarting")
		return
	}

	if h.conn == nil {
		writeHealthJSON(w, http.StatusServiceUnavailable, "NOT_READY",
			"gRPC connection not configured")
		return
	}

	state := h.conn.GetState()
	switch state {
	case connectivity.Ready, connectivity.Idle, connectivity.Connecting:
		// Acceptable states.
	default:
		detail := "orchestrator connection: " + state.String()
		slog.Warn("readiness check failed",
			"state", state.String(),
			"duration", time.Since(start),
		)
		writeHealthJSON(w, http.StatusServiceUnavailable, "NOT_READY", detail)
		return
	}

	h.smMu.RLock()
	managers := make(map[string]SecretManagerPinger, len(h.secretManagers))
	for k, v := range h.secretManagers {
		managers[k] = v
	}
	h.smMu.RUnlock()

	if len(managers) > 0 {
		pingCtx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()

		var g errgroup.Group
		var firstErrDetail string
		var firstErrMu sync.Mutex

		for name, sm := range managers {
			g.Go(func() error {
				if err := sm.Ping(pingCtx); err != nil {
					firstErrMu.Lock()
					if firstErrDetail == "" {
						firstErrDetail = fmt.Sprintf("secret manager %s unreachable: %v", name, err)
					}
					firstErrMu.Unlock()
					slog.Warn("readiness check: secret manager ping failed",
						"manager", name,
						"error", err,
					)
					return err
				}
				return nil
			})
		}

		if err := g.Wait(); err != nil {
			writeHealthJSON(w, http.StatusServiceUnavailable, "NOT_READY", firstErrDetail)
			return
		}
	}

	h.smMu.RLock()
	deps := make(map[string]func(context.Context) error, len(h.dependencies))
	for k, v := range h.dependencies {
		deps[k] = v
	}
	h.smMu.RUnlock()
	for name, check := range deps {
		depCtx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		err := check(depCtx)
		cancel()
		if err != nil {
			slog.Warn("readiness check: dependency unavailable", "dependency", name, "error", err)
			writeHealthJSON(w, http.StatusServiceUnavailable, "NOT_READY",
				fmt.Sprintf("%s unavailable: %v", name, err))
			return
		}
	}

	writeHealthJSON(w, http.StatusOK, "READY", "")
}

type healthResponse struct {
	Status string `json:"status"`
	Detail string `json:"detail,omitempty"`
	// Degraded lists the weaker modes the operator allowed and that are in use, with why: a gateway
	// running in one is ready, and says so every time it is asked.
	Degraded map[string]string `json:"degraded,omitempty"`
}

func writeHealthJSON(w http.ResponseWriter, httpStatus int, status, detail string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(httpStatus)
	_ = json.NewEncoder(w).Encode(healthResponse{Status: status, Detail: detail, Degraded: degraded.Active()})
}
