package monitoring

import (
	"context"
	"log/slog"
	"os"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.21.0"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

var (
	globalTracer     trace.Tracer
	globalTracerOnce sync.Once
	tracerShutdown   func(context.Context) error
)

// InitOTel initialises the global OTel TracerProvider and installs it.
// endpoint is the OTLP gRPC address, e.g. "otel-collector:4317".
// Returns a shutdown function the caller must invoke on exit.
func InitOTel(ctx context.Context, endpoint, serviceName, serviceVersion, environment string) (shutdown func(), err error) {
	var initErr error
	globalTracerOnce.Do(func() {
		conn, dialErr := grpc.NewClient(
			endpoint,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if dialErr != nil {
			initErr = dialErr
			return
		}

		exporter, expErr := otlptracegrpc.New(ctx,
			otlptracegrpc.WithGRPCConn(conn),
			otlptracegrpc.WithTimeout(5*time.Second),
		)
		if expErr != nil {
			initErr = expErr
			return
		}

		res := resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName(serviceName),
			semconv.ServiceVersion(serviceVersion),
			semconv.DeploymentEnvironment(environment),
		)

		tp := sdktrace.NewTracerProvider(
			sdktrace.WithBatcher(exporter),
			sdktrace.WithResource(res),
			sdktrace.WithSampler(sdktrace.AlwaysSample()),
		)

		otel.SetTracerProvider(tp)
		// Install W3C TraceContext + Baggage propagators so spans are linked
		// across gRPC and HTTP boundaries (gateway → orchestrator → watcher).
		otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
			propagation.TraceContext{},
			propagation.Baggage{},
		))
		globalTracer = tp.Tracer(serviceName)
		tracerShutdown = tp.Shutdown

		slog.Info("otel: tracer provider initialised", "endpoint", endpoint, "service", serviceName)
	})

	if initErr != nil {
		return func() {}, initErr
	}

	return func() {
		if tracerShutdown != nil {
			shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := tracerShutdown(shutCtx); err != nil {
				slog.Warn("otel: tracer shutdown error", "error", err)
			}
		}
	}, nil
}

// OTelEnabled returns true when OTel was successfully initialised.
func OTelEnabled() bool { return globalTracer != nil }

// OtelEnabledFromEnv returns true when OTEL_ENABLED=true.
func OtelEnabledFromEnv() bool {
	v := os.Getenv("OTEL_ENABLED")
	return v == "true" || v == "1" || v == "yes"
}
