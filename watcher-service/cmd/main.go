// watcher-service — VarTrack drift watcher with self-heal.
//
// It asks the orchestrator which rules to watch (ListWatchTargets: those with
// self_heal enabled that are not suspended), whether the datasource of each
// still matches Git (CheckDatasource), and asks it to re-sync the ones that
// drifted (SyncDatasource). The orchestrator layers the rules and reads the
// sinks; the watcher reads no rule and holds no sink credentials. It reads
// config.cue only for leader election.
//
// Startup sequence (mirrors gateway-service/cmd/main.go):
//
//  1. Load and validate environment variables.
//  2. Read the leader-election setting from the CUE config bundle.
//  3. Start the admin HTTP server (health + /metrics).
//  4. Run the watcher manager until SIGINT/SIGTERM; it asks the orchestrator
//     for its rules, and again on every poll.
//  5. Graceful shutdown.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"runtime/debug"
	"sync/atomic"
	"syscall"
	"time"

	"watcher-service/internal/config"
	"watcher-service/internal/election"
	"watcher-service/internal/healer"
	mon "watcher-service/internal/monitoring"
	"watcher-service/internal/watcher"
)

// Injected via -ldflags at build time.
var (
	version = "dev"
	commit  = "unknown"
)

func main() {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("fatal panic in main",
				"panic", fmt.Sprint(r),
				"stack", string(debug.Stack()),
			)
			panic(r) // let runtime print goroutine dump and exit with code 2
		}
	}()

	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// ── 1. Environment ────────────────────────────────────────────────────────
	env, err := config.LoadEnv()
	if err != nil {
		return fmt.Errorf("load env: %w", err)
	}

	slog.Info("starting watcher-service", "env", env)

	// ── 1b. OTel tracing ──────────────────────────────────────────────────────
	if mon.OtelEnabledFromEnv() {
		otelShutdown, otelErr := mon.InitOTel(ctx,
			getEnv("OTEL_ENDPOINT", "otel-collector:4317"),
			getEnv("OTEL_SERVICE_NAME", "vartrack-watcher"),
			getEnv("OTEL_SERVICE_VERSION", version),
			getEnv("OTEL_ENVIRONMENT", "demo"),
		)
		if otelErr != nil {
			slog.Warn("otel: init failed — tracing disabled", "error", otelErr)
		} else {
			defer otelShutdown()
		}
	}

	// ── 1c. ELK log shipping ──────────────────────────────────────────────────
	if mon.ElkEnabledFromEnv() {
		elkShutdown := mon.InitELK(
			[]string{getEnv("ES_ENDPOINTS", "http://elasticsearch:9200")},
			getEnv("ES_INDEX", "vartrack-logs"),
			getEnv("ELK_SERVICE_NAME", "vartrack-watcher"),
			version,
			getEnv("ELK_ENVIRONMENT", "demo"),
		)
		defer elkShutdown()
	}

	// ── 2. Metrics ────────────────────────────────────────────────────────────
	metrics := mon.DefaultMetrics()
	metrics.SetBuildInfo(version, commit, runtime.Version())

	// ── 3. Healer ─────────────────────────────────────────────────────────────
	h := healer.NewHealer(env.OrchestratorAddr, env.HealTimeout, env)

	pingCtx, pingCancel := context.WithTimeout(ctx, 5*time.Second)
	if err := h.Ping(pingCtx); err != nil {
		slog.Warn("watcher: orchestrator ping failed — healer may not work",
			"addr", env.OrchestratorAddr, "error", err)
	} else {
		slog.Info("watcher: orchestrator reachable", "addr", env.OrchestratorAddr)
	}
	pingCancel()

	// ── 4. Leader election ────────────────────────────────────────────────────
	// The only part of config.cue the watcher reads. The rules it watches come
	// from the orchestrator, which the manager asks on every poll.
	elCfg, err := config.LeaderElection(env.ConfigPath)
	if err != nil {
		return fmt.Errorf("leader election: %w", err)
	}
	manager := watcher.NewManager(h, env.PollInterval, metrics)

	// ── 5. Admin HTTP server ──────────────────────────────────────────────────
	var unavailable atomic.Bool
	adminSrv := newAdminServer(env.AdminAddr, metrics, manager, &unavailable)
	go func() {
		slog.Info("watcher: admin server listening", "addr", env.AdminAddr)
		if err := adminSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("watcher: admin server error", "error", err)
		}
	}()

	// ── 6. Signal handling + graceful shutdown ────────────────────────────────
	stopCh := make(chan os.Signal, 1)
	signal.Notify(stopCh, os.Interrupt, syscall.SIGTERM)

	managerDone := make(chan error, 1)

	switch {
	case elCfg != nil && len(elCfg.ZKHosts) > 0:
		elector, elErr := election.New(elCfg.ZKHosts, elCfg.ZKPath)
		if elErr != nil {
			return fmt.Errorf("leader election (zk): %w", elErr)
		}
		defer elector.Close()
		slog.Info("watcher: leader election enabled (ZooKeeper)",
			"zk_hosts", elCfg.ZKHosts, "path", elCfg.ZKPath)
		go func() {
			managerDone <- elector.Run(ctx, runManager(ctx, manager))
		}()

	case elCfg != nil && elCfg.RedisURL != "":
		elector, elErr := election.NewRedisElector(elCfg.RedisURL, "")
		if elErr != nil {
			return fmt.Errorf("leader election (redis): %w", elErr)
		}
		defer elector.Close()
		slog.Info("watcher: leader election enabled (Redis)")
		go func() {
			managerDone <- elector.Run(ctx, runManager(ctx, manager))
		}()

	default:
		slog.Info("watcher: leader election disabled — running as standalone instance")
		go func() {
			managerDone <- manager.Run(ctx)
		}()
	}

	select {
	case sig := <-stopCh:
		slog.Info("watcher: received shutdown signal", "signal", sig.String())
		cancel()

	case err := <-managerDone:
		if err != nil && !errors.Is(err, context.Canceled) {
			slog.Error("watcher: manager exited with error", "error", err)
		}
		cancel()
	}

	// Mark unavailable BEFORE shutdown so readiness probes return 503
	// while the service drains in-flight poll cycles.
	unavailable.Store(true)

	shutCtx, shutCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutCancel()
	if err := adminSrv.Shutdown(shutCtx); err != nil {
		slog.Warn("watcher: admin server shutdown error", "error", err)
	}

	// Wait for manager to finish.
	select {
	case <-managerDone:
	case <-time.After(5 * time.Second):
		slog.Warn("watcher: timed out waiting for manager to stop")
	}

	slog.Info("watcher: shutdown complete")
	return nil
}

// ── Admin HTTP server ─────────────────────────────────────────────────────────

func newAdminServer(
	addr string,
	metrics *mon.WatcherMetrics,
	mgr *watcher.Manager,
	unavailable *atomic.Bool,
) *http.Server {
	mux := http.NewServeMux()

	// GET /healthz and /health/liveness — always 200 while process is alive.
	liveness := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	}
	mux.HandleFunc("/healthz", liveness)
	mux.HandleFunc("/health/liveness", liveness)

	// GET /health/readiness — 503 when shutting down.
	mux.HandleFunc("/health/readiness", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if unavailable.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"status": "NOT_READY",
				"detail": "server is terminating",
			})
			return
		}
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"status":          "READY",
			"active_watchers": mgr.ActiveCount(),
		})
	})

	// GET /metrics — Prometheus text format
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		data, err := metrics.GenerateLatest()
		if err != nil {
			http.Error(w, "metrics gather error", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		_, _ = w.Write(data)
	})

	return &http.Server{
		Addr:         addr,
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
		IdleTimeout:  60 * time.Second,
	}
}

// runManager returns a leader-election callback that runs the watcher manager.
func runManager(_ context.Context, mgr *watcher.Manager) func(ctx context.Context) {
	return func(leaderCtx context.Context) {
		slog.Info("watcher: leading — manager started")
		if err := mgr.Run(leaderCtx); err != nil && !errors.Is(err, context.Canceled) {
			slog.Error("watcher: manager exited with error", "error", err)
		}
		slog.Info("watcher: leadership released — manager stopped")
	}
}

// getEnv returns the value of the named env var, or fallback when unset/empty.
func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
