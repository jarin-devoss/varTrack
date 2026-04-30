package monitoring

import (
	"context"
	"crypto/tls"
	"fmt"
	"log/slog"
	"time"

	monpb "gateway-service/internal/gen/proto/go/vartrack/v1/models/monitoring"
	"gateway-service/internal/protoutil"

	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/credentials"
)

// OTelBackend ships traces and metrics to any OTLP-compatible backend
// (Grafana Tempo, Honeycomb, Lightstep, a local OTel Collector, etc.).
//
// Implements:
//   - Backend                (Name / Ping / Shutdown)
//   - tracerProviderBackend  (TracerProvider)
//   - meterProviderBackend   (MeterProvider)
//   - propagatorBackend      (Propagator)
type OTelBackend struct {
	cfg            *monpb.OTelConfig
	tracerProvider *sdktrace.TracerProvider
	meterProvider  *sdkmetric.MeterProvider
}

// NewOTelBackend creates and starts the trace and metric exporter pipelines.
// Returns a disabled no-op backend (not an error) when enabled: false.
func NewOTelBackend(ctx context.Context, cfg *monpb.OTelConfig) (*OTelBackend, error) {
	b := &OTelBackend{cfg: cfg}

	if !cfg.GetEnabled() {
		slog.Info("otel: disabled")
		return b, nil
	}

	tlsCfg, err := buildBackendTLS(
		cfg.GetTlsCaCert(), cfg.GetTlsClientCert(), cfg.GetTlsClientKey(),
		cfg.GetInsecureSkipVerify(),
	)
	if err != nil {
		return nil, fmt.Errorf("otel: TLS: %w", err)
	}

	res, err := buildOTelResource(ctx,
		cfg.GetServiceName(), cfg.GetServiceVersion(), cfg.GetEnvironment(),
		cfg.GetResourceAttributes(),
	)
	if err != nil {
		return nil, fmt.Errorf("otel: resource: %w", err)
	}

	if cfg.GetEnableTraces() {
		tp, err := buildOTelTracerProvider(ctx, cfg, res, tlsCfg)
		if err != nil {
			return nil, fmt.Errorf("otel: trace provider: %w", err)
		}
		b.tracerProvider = tp
	}

	if cfg.GetEnableMetrics() {
		mp, err := buildOTelMeterProvider(ctx, cfg, res, tlsCfg)
		if err != nil {
			return nil, fmt.Errorf("otel: metric provider: %w", err)
		}
		b.meterProvider = mp
	}

	slog.Info("otel: started",
		"endpoint", cfg.GetEndpoint(),
		"protocol", cfg.GetProtocol(),
		"traces", cfg.GetEnableTraces(),
		"metrics", cfg.GetEnableMetrics(),
	)
	return b, nil
}

func (b *OTelBackend) Name() string { return "otel" }

// TracerProvider satisfies tracerProviderBackend.
func (b *OTelBackend) TracerProvider() trace.TracerProvider {
	if b.tracerProvider == nil {
		return nil
	}
	return b.tracerProvider
}

// MeterProvider satisfies meterProviderBackend.
func (b *OTelBackend) MeterProvider() metric.MeterProvider {
	if b.meterProvider == nil {
		return nil
	}
	return b.meterProvider
}

// Propagator satisfies propagatorBackend.
// Builds from the configured propagators list; defaults to TraceContext + Baggage.
func (b *OTelBackend) Propagator() propagation.TextMapPropagator {
	var props []propagation.TextMapPropagator
	for _, name := range b.cfg.GetPropagators() {
		switch name {
		case "tracecontext":
			props = append(props, propagation.TraceContext{})
		case "baggage":
			props = append(props, propagation.Baggage{})
		default:
			slog.Warn("otel: unknown propagator ignored", "name", name)
		}
	}
	if len(props) == 0 {
		props = []propagation.TextMapPropagator{
			propagation.TraceContext{},
			propagation.Baggage{},
		}
	}
	return propagation.NewCompositeTextMapPropagator(props...)
}

func (b *OTelBackend) Ping(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	if b.tracerProvider != nil {
		if err := b.tracerProvider.ForceFlush(ctx); err != nil {
			return fmt.Errorf("otel ping (traces): %w", err)
		}
	}
	if b.meterProvider != nil {
		if err := b.meterProvider.ForceFlush(ctx); err != nil {
			return fmt.Errorf("otel ping (metrics): %w", err)
		}
	}
	return nil
}

func (b *OTelBackend) Shutdown(ctx context.Context) error {
	var errs []error
	if b.tracerProvider != nil {
		if err := b.tracerProvider.Shutdown(ctx); err != nil {
			errs = append(errs, fmt.Errorf("traces: %w", err))
		}
	}
	if b.meterProvider != nil {
		if err := b.meterProvider.Shutdown(ctx); err != nil {
			errs = append(errs, fmt.Errorf("metrics: %w", err))
		}
	}
	if len(errs) > 0 {
		return fmt.Errorf("otel shutdown: %v", errs)
	}
	slog.Info("otel: shut down")
	return nil
}

// ── trace provider ────────────────────────────────────────────────────────────

func buildOTelTracerProvider(
	ctx context.Context,
	cfg *monpb.OTelConfig,
	res *resource.Resource,
	tlsCfg *tls.Config,
) (*sdktrace.TracerProvider, error) {
	var exp sdktrace.SpanExporter
	var err error

	switch cfg.GetProtocol() {
	case "grpc", "":
		opts := []otlptracegrpc.Option{
			otlptracegrpc.WithEndpoint(cfg.GetEndpoint()),
			otlptracegrpc.WithHeaders(cfg.GetHeaders()),
		}
		if cfg.GetUseTls() {
			opts = append(opts, otlptracegrpc.WithTLSCredentials(credentials.NewTLS(tlsCfg)))
		} else {
			opts = append(opts, otlptracegrpc.WithInsecure())
		}
		exp, err = otlptracegrpc.New(ctx, opts...)

	case "http/protobuf", "http/json":
		opts := []otlptracehttp.Option{
			otlptracehttp.WithEndpoint(cfg.GetEndpoint()),
			otlptracehttp.WithHeaders(cfg.GetHeaders()),
		}
		if !cfg.GetUseTls() {
			opts = append(opts, otlptracehttp.WithInsecure())
		}
		exp, err = otlptracehttp.New(ctx, opts...)

	default:
		return nil, fmt.Errorf("unsupported trace protocol %q", cfg.GetProtocol())
	}
	if err != nil {
		return nil, err
	}

	batchOpts := []sdktrace.BatchSpanProcessorOption{}
	if d := protoutil.DurationOrDefault(cfg.GetBatchTimeout(), 0); d > 0 {
		batchOpts = append(batchOpts, sdktrace.WithBatchTimeout(d))
	}
	if v := cfg.GetMaxQueueSize(); v > 0 {
		batchOpts = append(batchOpts, sdktrace.WithMaxQueueSize(int(v)))
	}
	if v := cfg.GetMaxExportBatch(); v > 0 {
		batchOpts = append(batchOpts, sdktrace.WithMaxExportBatchSize(int(v)))
	}

	return sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp, batchOpts...),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(buildOTelSampler(cfg)),
	), nil
}

// ── metric provider ───────────────────────────────────────────────────────────

func buildOTelMeterProvider(
	ctx context.Context,
	cfg *monpb.OTelConfig,
	res *resource.Resource,
	tlsCfg *tls.Config,
) (*sdkmetric.MeterProvider, error) {
	interval := protoutil.DurationOrDefault(cfg.GetMetricsInterval(), 60*time.Second)

	var reader sdkmetric.Reader

	switch cfg.GetProtocol() {
	case "grpc", "":
		opts := []otlpmetricgrpc.Option{
			otlpmetricgrpc.WithEndpoint(cfg.GetEndpoint()),
			otlpmetricgrpc.WithHeaders(cfg.GetHeaders()),
		}
		if cfg.GetUseTls() {
			opts = append(opts, otlpmetricgrpc.WithTLSCredentials(credentials.NewTLS(tlsCfg)))
		} else {
			opts = append(opts, otlpmetricgrpc.WithInsecure())
		}
		exp, err := otlpmetricgrpc.New(ctx, opts...)
		if err != nil {
			return nil, err
		}
		reader = sdkmetric.NewPeriodicReader(exp, sdkmetric.WithInterval(interval))

	case "http/protobuf", "http/json":
		opts := []otlpmetrichttp.Option{
			otlpmetrichttp.WithEndpoint(cfg.GetEndpoint()),
			otlpmetrichttp.WithHeaders(cfg.GetHeaders()),
		}
		if !cfg.GetUseTls() {
			opts = append(opts, otlpmetrichttp.WithInsecure())
		}
		exp, err := otlpmetrichttp.New(ctx, opts...)
		if err != nil {
			return nil, err
		}
		reader = sdkmetric.NewPeriodicReader(exp, sdkmetric.WithInterval(interval))

	default:
		return nil, fmt.Errorf("unsupported metric protocol %q", cfg.GetProtocol())
	}

	return sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(reader),
		sdkmetric.WithResource(res),
	), nil
}

func buildOTelSampler(cfg *monpb.OTelConfig) sdktrace.Sampler {
	switch cfg.GetSamplerType() {
	case "always_on":
		return sdktrace.AlwaysSample()
	case "always_off":
		return sdktrace.NeverSample()
	case "traceidratio":
		return sdktrace.TraceIDRatioBased(cfg.GetSamplerRatio())
	case "parentbased_always_on":
		return sdktrace.ParentBased(sdktrace.AlwaysSample())
	case "parentbased_always_off":
		return sdktrace.ParentBased(sdktrace.NeverSample())
	case "parentbased_traceidratio":
		return sdktrace.ParentBased(sdktrace.TraceIDRatioBased(cfg.GetSamplerRatio()))
	default:
		return sdktrace.ParentBased(sdktrace.AlwaysSample())
	}
}
