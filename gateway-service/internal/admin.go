// Package internal wires the HTTP server, admin server, router, and TLS
// configuration for the gateway service.
package internal

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/pprof"
	"time"

	"gateway-service/internal/handlers"
)

// AdminServer runs internal endpoints (health, pprof, metrics) on a
// separate listener isolated from public webhook traffic.
type AdminServer struct {
	server        *http.Server
	healthHandler *handlers.HealthHandler
}

// AdminConfig configures the admin server.
type AdminConfig struct {
	Addr           string
	EnablePprof    bool
	MetricsHandler http.Handler // optional — mounted at /metrics when non-nil

	// OPAReload, when non-nil, is mounted at POST /admin/opa/reload.
	OPAReload func(context.Context) error
}

// NewAdminServer creates an admin server with health, metrics, and optional debug endpoints.
func NewAdminServer(cfg AdminConfig, healthHandler *handlers.HealthHandler) *AdminServer {
	mux := http.NewServeMux()

	mux.HandleFunc("/healthz", healthHandler.Readiness)
	mux.HandleFunc("GET /health/liveness", healthHandler.Liveness)
	mux.HandleFunc("GET /health/readiness", healthHandler.Readiness)

	if cfg.MetricsHandler != nil {
		mux.Handle("/metrics", cfg.MetricsHandler)
	}

	if cfg.OPAReload != nil {
		mux.HandleFunc("POST /admin/opa/reload", opaReloadHandler(cfg.OPAReload))
	}

	if cfg.EnablePprof {
		mux.HandleFunc("/debug/pprof/", pprof.Index)
		mux.HandleFunc("/debug/pprof/cmdline", pprof.Cmdline)
		mux.HandleFunc("/debug/pprof/profile", pprof.Profile)
		mux.HandleFunc("/debug/pprof/symbol", pprof.Symbol)
		mux.HandleFunc("/debug/pprof/trace", pprof.Trace)
		mux.Handle("/debug/pprof/goroutine", pprof.Handler("goroutine"))
		mux.Handle("/debug/pprof/heap", pprof.Handler("heap"))
		mux.Handle("/debug/pprof/threadcreate", pprof.Handler("threadcreate"))
		mux.Handle("/debug/pprof/block", pprof.Handler("block"))
	}

	return &AdminServer{
		server: &http.Server{
			Addr:              cfg.Addr,
			Handler:           mux,
			ReadHeaderTimeout: 5 * time.Second,
			ReadTimeout:       10 * time.Second,
			WriteTimeout:      30 * time.Second, // pprof profiles can take time
			IdleTimeout:       60 * time.Second,
		},
		healthHandler: healthHandler,
	}
}

// opaReloadHandler recompiles the OPA policy. On failure the previous policy
// stays in force and the compile error is returned to the caller.
func opaReloadHandler(reload func(context.Context) error) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if err := reload(r.Context()); err != nil {
			slog.Warn("OPA policy reload failed", "error", err)
			w.WriteHeader(http.StatusUnprocessableEntity)
			_ = json.NewEncoder(w).Encode(map[string]string{"detail": err.Error()})
			return
		}
		slog.Info("OPA policy reloaded")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "reloaded"})
	}
}

// Serve starts the admin server. It blocks until the server stops.
func (a *AdminServer) Serve() error {
	slog.Info("admin server starting", "addr", a.server.Addr)
	err := a.server.ListenAndServe()
	if err != nil && err != http.ErrServerClosed {
		return fmt.Errorf("admin server error: %w", err)
	}
	return nil
}

// Shutdown gracefully stops the admin server.
func (a *AdminServer) Shutdown(ctx context.Context) error {
	return a.server.Shutdown(ctx)
}
