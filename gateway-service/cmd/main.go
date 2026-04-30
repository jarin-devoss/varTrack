package main

import (
	"context"
	"fmt"
	"log"
	"log/slog"
	"os"
	"os/signal"
	"runtime/debug"
	"syscall"
	"time"

	"gateway-service/internal"
	"gateway-service/internal/config"
	"gateway-service/internal/middlewares"
	"gateway-service/internal/secrets"

	pb "gateway-service/internal/gen/proto/go/vartrack/v1/services"

	mon "gateway-service/internal/monitoring"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"google.golang.org/grpc"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/keepalive"
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
				panic(r)
		}
	}()

	if err := run(); err != nil {
		log.Fatal(err)
	}
}

// run contains all application logic so that defers run correctly and
// there is a single exit point (log.Fatal) in main().
func run() error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	slog.SetDefault(slog.New(secrets.NewMaskingHandler(slog.Default().Handler())))

	// 1. Load and validate environment variables.
	env, err := config.LoadEnv()
	if err != nil {
		return fmt.Errorf("load environment config: %w", err)
	}

	// env.LogValue() masks secrets automatically via slog.LogValuer.
	slog.Info("starting gateway-service", "env", env)

	// 1b. OTel tracing — send spans to otel-collector → Jaeger.
	if mon.OtelEnabledFromEnv() {
		otelShutdown, otelErr := mon.InitOTel(ctx,
			config.EnvOr("OTEL_ENDPOINT", "otel-collector:4317"),
			config.EnvOr("OTEL_SERVICE_NAME", "vartrack-gateway"),
			config.EnvOr("OTEL_SERVICE_VERSION", version),
			config.EnvOr("OTEL_ENVIRONMENT", "demo"),
		)
		if otelErr != nil {
			slog.Warn("otel: init failed — tracing disabled", "error", otelErr)
		} else {
			defer otelShutdown()
		}
	}

	// 1c. ELK logging — ship slog records to Elasticsearch.
	if mon.ElkEnabledFromEnv() {
		elkShutdown := mon.InitELK(
			[]string{config.EnvOr("ES_ENDPOINTS", "http://elasticsearch:9200")},
			config.EnvOr("ES_INDEX", "vartrack-logs"),
			config.EnvOr("ELK_SERVICE_NAME", "vartrack-gateway"),
			version,
			config.EnvOr("ELK_ENVIRONMENT", "demo"),
		)
		defer elkShutdown()
	}

	// 2. Load bundle from CUE file.
	bundleService, err := config.NewBundle(env.ConfigPath)
	if err != nil {
		return fmt.Errorf("load config from %s: %w", env.ConfigPath, err)
	}

	// 3. Connect to orchestrator with resilience.
	transportCreds, err := buildTransportCredentials(env)
	if err != nil {
		return fmt.Errorf("build transport credentials: %w", err)
	}

	conn, err := grpc.NewClient(
		env.GetOrchestratorAddr(),
		grpc.WithTransportCredentials(transportCreds),
		grpc.WithKeepaliveParams(keepalive.ClientParameters{
			Time:                20 * time.Second,
			Timeout:             5 * time.Second,
			PermitWithoutStream: true,
		}),
		grpc.WithUserAgent("gateway-service"),
		// Propagate W3C TraceContext + Baggage from gateway spans into gRPC metadata
		// so orchestrator spans become children of gateway spans in Jaeger.
		grpc.WithStatsHandler(otelgrpc.NewClientHandler()),
	)
	if err != nil {
		return fmt.Errorf("connect to orchestrator: %w", err)
	}

	grpcClient := pb.NewOrchestratorClient(conn)

	// Block until the orchestrator connection proves it's alive, or timeout
	// fail-fast to prevent misconfigured instances from passing probes.
	slog.Info("dialing orchestrator...", "addr", env.GetOrchestratorAddr())
	conn.Connect()
	connectCtx, connectCancel := context.WithTimeout(ctx, 10*time.Second)
	defer connectCancel()

	for {
		state := conn.GetState()
		if state == connectivity.Ready {
			break
		}
		if !conn.WaitForStateChange(connectCtx, state) {
			return fmt.Errorf("timeout waiting for orchestrator connection: %v", connectCtx.Err())
		}
	}
	slog.Info("orchestrator connection ready")

	// 4. Wire router.
	routerOpts := []internal.RouterOption{internal.WithProduction(env.IsProduction())}
	if env.RedisURL != "" {
		ns, nsErr := middlewares.NewRedisNonceStore(env.RedisURL)
		if nsErr != nil {
			slog.Warn("gateway: Redis nonce store unavailable — using in-process memory store",
				"error", nsErr)
		} else {
			routerOpts = append(routerOpts, internal.WithNonceStore(ns))
			slog.Info("gateway: distributed Redis nonce store enabled")
		}
	}
	r := internal.NewRouter(bundleService, grpcClient, conn, routerOpts...)

	// 5. Start admin server on a separate port.
	adminAddr := config.EnvOr("ADMIN_ADDR", ":9090")
	adminSrv := internal.NewAdminServer(internal.AdminConfig{
		Addr:           adminAddr,
		EnablePprof:    !env.IsProduction(),
		MetricsHandler: mon.DefaultMetrics().Handler(),
	}, r.HealthHandler())

	go func() {
		if err := adminSrv.Serve(); err != nil {
			slog.Error("admin server error", "error", err)
		}
	}()

	// 6. Graceful shutdown.
	//
	// Sequence:
	//   a) Mark the service unavailable (LB stops routing new requests).
	//   b) Shut down the admin server.
	//   c) Cancel the main context → triggers HTTP server graceful shutdown.
	//   d) Wait for the HTTP server to finish draining.
	//   e) Close the bundle (Vault connections, platform clients).
	//   f) Drain and close the gRPC connection.
	stopCh := make(chan os.Signal, 1)
	signal.Notify(stopCh, os.Interrupt, syscall.SIGTERM)

	serverDone := make(chan struct{})
	cleanupDone := make(chan struct{})

	go func() {
		sig := <-stopCh
		slog.Info("received shutdown signal", "signal", sig.String())

		r.SetUnavailable()

		adminCtx, adminCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer adminCancel()
		if err := adminSrv.Shutdown(adminCtx); err != nil {
			slog.Error("admin server shutdown error", "error", err)
		}

		cancel()
		select {
		case <-serverDone:
			slog.Info("HTTP server drained and stopped")
		case <-time.After(25 * time.Second):
			slog.Warn("timeout waiting for HTTP server to drain")
		}

		closeCtx, closeCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer closeCancel()
		if err := bundleService.Close(closeCtx); err != nil {
			slog.Error("bundle close error", "error", err)
		}

		drainCtx, drainCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer drainCancel()
		if err := shutdownGRPCConnection(drainCtx, conn); err != nil {
			slog.Warn("gRPC drain did not complete cleanly", "error", err)
			conn.Close()
		}
		close(cleanupDone)
	}()

	// 7. Resolve inbound TLS config.
	tlsCfg := resolveInboundTLS(env)

	internal.Run(ctx, env.GetGatewayAddr(), r, tlsCfg)
	close(serverDone)
	<-cleanupDone
	return nil
}

// resolveInboundTLS builds the inbound TLS config from the loaded env object.
func resolveInboundTLS(env *config.Env) *internal.TLSConfig {
	cert, key := env.GetTLSCert(), env.GetTLSKey()

	if cert != "" && key != "" {
		slog.Info("inbound TLS: loading certificate from files",
			"cert", cert, "key", key)
		return &internal.TLSConfig{CertFile: cert, KeyFile: key}
	}

	if !env.IsProduction() {
		slog.Info("inbound TLS: disabled (non-production)")
		return nil
	}

	slog.Info("inbound TLS: disabled (expects TLS termination upstream)")
	return nil
}
