package monitoring

import (
	"context"
	"crypto/tls"
	"fmt"
	"log/slog"
	"time"

	monpb "gateway-service/internal/gen/proto/go/vartrack/v1/models/monitoring"
	"gateway-service/internal/protoutil"

	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/encoding/gzip"
)

// JaegerBackend ships spans to a Jaeger collector via OTLP/gRPC or OTLP/HTTP.
//
// Implements:
//   - Backend            (Name / Ping / Shutdown)
//   - tracerProviderBackend  (TracerProvider)
//   - propagatorBackend      (Propagator — installs W3C TraceContext + Baggage)
//
// When monitoring.Init() receives this backend it installs the TracerProvider
// globally, which makes otelhttp and otelgrpc auto-instrumentation work with
// zero additional configuration at call sites.
type JaegerBackend struct {
	cfg      *monpb.JaegerConfig
	provider *sdktrace.TracerProvider
}

// NewJaegerBackend creates and starts the Jaeger exporter pipeline.
// Returns a disabled no-op backend (not an error) when enabled: false.
func NewJaegerBackend(ctx context.Context, cfg *monpb.JaegerConfig) (*JaegerBackend, error) {
	b := &JaegerBackend{cfg: cfg}

	if !cfg.GetEnabled() {
		slog.Info("jaeger: disabled")
		return b, nil
	}

	tlsCfg, err := buildBackendTLS(
		cfg.GetTlsCaCert(), cfg.GetTlsClientCert(), cfg.GetTlsClientKey(),
		cfg.GetInsecureSkipVerify(),
	)
	if err != nil {
		return nil, fmt.Errorf("jaeger: TLS: %w", err)
	}

	exp, err := b.buildExporter(ctx, tlsCfg)
	if err != nil {
		return nil, fmt.Errorf("jaeger: exporter: %w", err)
	}

	res, err := buildOTelResource(ctx,
		cfg.GetServiceName(), cfg.GetServiceVersion(), cfg.GetEnvironment(),
		cfg.GetResourceAttributes(),
	)
	if err != nil {
		return nil, fmt.Errorf("jaeger: resource: %w", err)
	}

	batchOpts := []sdktrace.BatchSpanProcessorOption{}
	if v := cfg.GetMaxQueueSize(); v > 0 {
		batchOpts = append(batchOpts, sdktrace.WithMaxQueueSize(int(v)))
	}
	if d := protoutil.DurationOrDefault(cfg.GetFlushInterval(), 0); d > 0 {
		batchOpts = append(batchOpts, sdktrace.WithBatchTimeout(d))
	}

	b.provider = sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp, batchOpts...),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(buildJaegerSampler(cfg)),
	)

	slog.Info("jaeger: started",
		"endpoint", cfg.GetEndpoint(),
		"protocol", cfg.GetProtocol(),
		"service", cfg.GetServiceName(),
		"sampler", cfg.GetSamplerType(),
	)
	return b, nil
}

func (b *JaegerBackend) Name() string { return "jaeger" }

// TracerProvider satisfies tracerProviderBackend.
// monitoring.Init() installs this as the global OTel TracerProvider.
func (b *JaegerBackend) TracerProvider() trace.TracerProvider {
	if b.provider == nil {
		return nil
	}
	return b.provider
}

// Propagator installs W3C TraceContext + Baggage so otelhttp reads the incoming
// traceparent header and otelgrpc injects it into outbound calls.
func (b *JaegerBackend) Propagator() propagation.TextMapPropagator {
	return propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	)
}

// Ping force-flushes buffered spans.  Used by the background health logger.
func (b *JaegerBackend) Ping(ctx context.Context) error {
	if b.provider == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := b.provider.ForceFlush(ctx); err != nil {
		return fmt.Errorf("jaeger ping: %w", err)
	}
	return nil
}

// Shutdown flushes remaining spans and closes the exporter.
func (b *JaegerBackend) Shutdown(ctx context.Context) error {
	if b.provider == nil {
		return nil
	}
	if err := b.provider.Shutdown(ctx); err != nil {
		return fmt.Errorf("jaeger shutdown: %w", err)
	}
	slog.Info("jaeger: shut down")
	return nil
}

// ── exporter factory ──────────────────────────────────────────────────────────

func (b *JaegerBackend) buildExporter(ctx context.Context, tlsCfg *tls.Config) (sdktrace.SpanExporter, error) {
	switch b.cfg.GetProtocol() {
	case "grpc", "":
		opts := []otlptracegrpc.Option{
			otlptracegrpc.WithEndpoint(b.cfg.GetEndpoint()),
			otlptracegrpc.WithCompressor(gzip.Name), // compress spans; saves bandwidth at scale
		}
		if b.cfg.GetUseTls() {
			opts = append(opts, otlptracegrpc.WithTLSCredentials(credentials.NewTLS(tlsCfg)))
		} else {
			opts = append(opts, otlptracegrpc.WithDialOption(
				grpc.WithTransportCredentials(insecure.NewCredentials()),
			))
		}
		if t := b.cfg.GetAuthToken(); t != "" {
			opts = append(opts, otlptracegrpc.WithHeaders(map[string]string{
				"Authorization": "Bearer " + t,
			}))
		}
		return otlptracegrpc.New(ctx, opts...)

	case "thrift_http":
		opts := []otlptracehttp.Option{
			otlptracehttp.WithEndpoint(b.cfg.GetEndpoint()),
		}
		if !b.cfg.GetUseTls() {
			opts = append(opts, otlptracehttp.WithInsecure())
		}
		if t := b.cfg.GetAuthToken(); t != "" {
			opts = append(opts, otlptracehttp.WithHeaders(map[string]string{
				"Authorization": "Bearer " + t,
			}))
		}
		return otlptracehttp.New(ctx, opts...)

	default:
		return nil, fmt.Errorf("unsupported protocol %q — use grpc or thrift_http", b.cfg.GetProtocol())
	}
}

// ── sampler mapping ───────────────────────────────────────────────────────────

func buildJaegerSampler(cfg *monpb.JaegerConfig) sdktrace.Sampler {
	switch cfg.GetSamplerType() {
	case "const":
		if cfg.GetSamplerParam() == 0 {
			return sdktrace.NeverSample()
		}
		return sdktrace.AlwaysSample()
	case "probabilistic":
		return sdktrace.ParentBased(sdktrace.TraceIDRatioBased(cfg.GetSamplerParam()))
	case "rateLimiting":
		// OTel SDK has no rate-limiting sampler; approximate with ratio (param = req/s ÷ 1000).
		return sdktrace.ParentBased(sdktrace.TraceIDRatioBased(cfg.GetSamplerParam() / 1000))
	default:
		return sdktrace.ParentBased(sdktrace.AlwaysSample())
	}
}
