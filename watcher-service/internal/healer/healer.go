// Package healer triggers the orchestrator to re-sync a datasource via gRPC.
//
// After drift is detected the watcher calls SyncDatasource on the orchestrator's
// Watcher gRPC service.  The orchestrator enqueues a Celery sync_all_task and
// returns immediately; the watcher does not wait for the task to complete —
// it will confirm healing on the next poll cycle.
//
// Proto definition: proto/vartrack/v1/services/watcher.proto
// Generated stubs:  watcher-service/internal/gen/proto/go/vartrack/v1/services/
//
// To regenerate stubs after proto changes:
//
//	buf generate --template buf.gen.watcher.yaml
package healer

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log/slog"
	"os"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"golang.org/x/sync/singleflight"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/emptypb"

	"watcher-service/internal/config"
	pb "watcher-service/internal/gen/proto/go/vartrack/v1/services"
)

// correlationIDKey is the context key for the X-Correlation-ID value.
type correlationIDKey struct{}

// WithCorrelationID returns a context carrying the given correlation ID.
func WithCorrelationID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, correlationIDKey{}, id)
}

// HealRequest is the parameters for a self-heal operation.
type HealRequest struct {
	// Datasource is the datasource name in the CUE bundle (e.g. "mongo").
	Datasource string

	// Platform is the platform name in the CUE bundle (e.g. "github").
	Platform string

	// Tenant optionally restricts the sync to a single tenant.
	// Empty string means "all tenants managed by this rule".
	Tenant string

	// Env optionally restricts the sync to a single environment.
	Env string

	// Reason is a human-readable explanation logged by the orchestrator.
	Reason string
}

// Healer calls the orchestrator's Watcher gRPC service to trigger self-heal.
type Healer struct {
	addr    string
	timeout time.Duration
	conn    *grpc.ClientConn
	client  pb.WatcherClient
	breaker *circuitBreaker

	// in-process ping micro-cache — avoids hitting the orchestrator health
	// endpoint on every poll cycle (can be 60+ times/minute with many watchers).
	_pingOK       atomic.Bool
	_pingCachedAt atomic.Int64 // UnixNano of last successful ping
	_pingSF singleflight.Group
}

// NewHealer creates a Healer that dials addr (e.g. "localhost:50051").
//
// TLS is configured from env:
//   - TLSCAFile                        — verify orchestrator server cert
//   - TLSCertFile + TLSKeyFile         — client cert for mTLS
//   - all empty                        — insecure (plaintext, safe for local dev)
func NewHealer(addr string, timeout time.Duration, env *config.Env) *Healer {
	creds := buildCredentials(env)

	conn, err := grpc.NewClient(addr,
		grpc.WithTransportCredentials(creds),
		grpc.WithBlock(),
		grpc.WithTimeout(5*time.Second),
	)
	if err != nil {
		slog.Error("healer: failed to dial orchestrator", "addr", addr, "err", err)
		// Return a healer with a nil conn; Heal/Ping will surface the error.
		return &Healer{addr: addr, timeout: timeout, breaker: newCircuitBreaker()}
	}

	return &Healer{
		addr:    addr,
		timeout: timeout,
		conn:    conn,
		client:  pb.NewWatcherClient(conn),
		breaker: newCircuitBreaker(),
	}
}

// buildCredentials returns gRPC transport credentials from env TLS config.
// Falls back to insecure when no TLS fields are set.
func buildCredentials(env *config.Env) credentials.TransportCredentials {
	if env == nil || (env.TLSCAFile == "" && env.TLSCertFile == "") {
		return insecure.NewCredentials()
	}

	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}

	// Load CA to verify the orchestrator's server certificate.
	if env.TLSCAFile != "" {
		caPEM, err := os.ReadFile(env.TLSCAFile)
		if err != nil {
			slog.Error("healer: failed to read TLS CA file — using system pool", "file", env.TLSCAFile, "err", err)
		} else {
			pool := x509.NewCertPool()
			if !pool.AppendCertsFromPEM(caPEM) {
				slog.Error("healer: failed to parse CA cert — using system pool", "file", env.TLSCAFile)
			} else {
				tlsCfg.RootCAs = pool
			}
		}
	}

	// Load client certificate for mTLS.
	if env.TLSCertFile != "" && env.TLSKeyFile != "" {
		cert, err := tls.LoadX509KeyPair(env.TLSCertFile, env.TLSKeyFile)
		if err != nil {
			slog.Error("healer: failed to load client TLS keypair — proceeding without client cert",
				"cert", env.TLSCertFile, "err", err)
		} else {
			tlsCfg.Certificates = []tls.Certificate{cert}
			slog.Info("healer: mTLS client certificate loaded", "cert", env.TLSCertFile)
		}
	}

	return credentials.NewTLS(tlsCfg)
}

// Heal sends a SyncDatasource request to the orchestrator and waits for acknowledgement.
//
// The orchestrator enqueues a Celery task and returns immediately with a
// task_id; the watcher does not wait for the task to complete.  The
// reconciliation loop will detect and confirm healing on the next poll.
func (h *Healer) Heal(ctx context.Context, req HealRequest) error {
	if h.client == nil {
		return fmt.Errorf("healer: gRPC client not initialised (dial failed at startup)")
	}
	if !h.breaker.allow() {
		return fmt.Errorf("healer: circuit breaker open — orchestrator unreachable, skipping heal for datasource=%s", req.Datasource)
	}

	if req.Reason == "" {
		req.Reason = "self-heal: drift detected by watcher-service"
	}

	rctx, cancel := context.WithTimeout(ctx, h.timeout)
	defer cancel()

	// Propagate W3C TraceContext so watcher spans link to orchestrator spans.
	md := metadata.MD{}
	otel.GetTextMapPropagator().Inject(rctx, metadataCarrier(md))

	// Propagate correlation ID as gRPC metadata.
	if cid, ok := ctx.Value(correlationIDKey{}).(string); ok && cid != "" {
		md.Set("x-correlation-id", cid)
	}

	if len(md) > 0 {
		rctx = metadata.NewOutgoingContext(rctx, md)
	}

	resp, err := h.client.SyncDatasource(rctx, &pb.SyncDatasourceRequest{
		Datasource: req.Datasource,
		Platform:   req.Platform,
		Tenant:     req.Tenant,
		Env:        req.Env,
		Reason:     req.Reason,
	})
	if err != nil {
		h.breaker.recordFailure()
		return fmt.Errorf("healer: SyncDatasource %s: %w", req.Datasource, err)
	}

	h.breaker.recordSuccess()
	slog.Info("healer: heal task enqueued",
		"datasource", req.Datasource,
		"task_id", resp.TaskId,
		"status", resp.Status,
	)
	return nil
}

// Ping checks the orchestrator liveness via the Watcher.Ping RPC.
//
// Results are cached for 5 seconds so that multiple watcher goroutines
// polling concurrently do not flood the orchestrator.  The cache is
// invalidated on any error so failures propagate immediately.
func (h *Healer) Ping(ctx context.Context) error {
	if h.client == nil {
		return fmt.Errorf("healer: gRPC client not initialised (dial failed at startup)")
	}

	const cacheTTL = 5 * time.Second

	// Fast path: return cached OK result if it's fresh.
	if h._pingOK.Load() {
		cachedAt := time.Unix(0, h._pingCachedAt.Load())
		if time.Since(cachedAt) < cacheTTL {
			return nil
		}
	}

	// Slow path: coalesce concurrent pings via singleflight.
	_, err, _ := h._pingSF.Do("ping", func() (any, error) {
		// Use a fresh background context so one caller's cancelled ctx doesn't
		// fail the shared flight that all other goroutines are waiting on.
		pCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		_, err := h.client.Ping(pCtx, &emptypb.Empty{})
		if err != nil {
			h._pingOK.Store(false)
			return nil, fmt.Errorf("healer ping: %w", err)
		}

		h._pingOK.Store(true)
		h._pingCachedAt.Store(time.Now().UnixNano())
		return nil, nil
	})
	return err
}

// Close shuts down the underlying gRPC connection.
func (h *Healer) Close() {
	if h.conn != nil {
		h.conn.Close()
	}
}

// metadataCarrier adapts grpc/metadata.MD to the otel TextMapCarrier interface.
type metadataCarrier metadata.MD

func (mc metadataCarrier) Get(key string) string {
	vals := metadata.MD(mc).Get(key)
	if len(vals) == 0 {
		return ""
	}
	return vals[0]
}

func (mc metadataCarrier) Set(key, val string) {
	metadata.MD(mc).Set(key, val)
}

func (mc metadataCarrier) Keys() []string {
	keys := make([]string, 0, len(mc))
	for k := range mc {
		keys = append(keys, k)
	}
	return keys
}

// Ensure metadataCarrier satisfies propagation.TextMapCarrier at compile time.
var _ propagation.TextMapCarrier = metadataCarrier{}
