package monitoring

import (
	"context"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// Span represents a single traced operation.
// When OTel is initialised via InitOTel() it wraps a real trace.Span and
// exports to the configured backend.  Without OTel it degrades to structured
// slog entries with duration — still observable, zero extra dependencies.
type Span struct {
	otelSpan  trace.Span   // non-nil when OTel is active
	ctx       context.Context
	name      string
	startTime time.Time
	attrs     map[string]any
}

// End finalises the span. If err is non-nil the span is marked as errored.
func (s *Span) End(err error) {
	dur := time.Since(s.startTime)
	if s.otelSpan != nil {
		if err != nil {
			s.otelSpan.RecordError(err)
			s.otelSpan.SetStatus(codes.Error, err.Error())
		} else {
			s.otelSpan.SetStatus(codes.Ok, "")
		}
		s.otelSpan.End()
		return
	}
	// Fallback: structured log entry.
	if err != nil {
		slog.Debug("trace span finished with error",
			"span", s.name, "duration_ms", dur.Milliseconds(), "error", err)
		return
	}
	slog.Debug("trace span finished", "span", s.name, "duration_ms", dur.Milliseconds())
}

// SetAttr attaches a key-value attribute to the span.
func (s *Span) SetAttr(key string, value any) {
	if s.otelSpan != nil {
		s.otelSpan.SetAttributes(attribute.String(key, asString(value)))
		return
	}
	if s.attrs == nil {
		s.attrs = make(map[string]any)
	}
	s.attrs[key] = value
}

// Start creates a new Span.  Callers must call span.End(err) when the
// operation finishes (typically via defer).
//
//	ctx, span := monitoring.Start(ctx, "webhook.verify")
//	defer span.End(err)
func Start(ctx context.Context, name string) (context.Context, *Span) {
	if globalTracer != nil {
		otelCtx, otelSpan := globalTracer.Start(ctx, name)
		return otelCtx, &Span{otelSpan: otelSpan, ctx: otelCtx, name: name, startTime: time.Now()}
	}
	s := &Span{ctx: ctx, name: name, startTime: time.Now()}
	slog.Debug("trace span started", "span", name)
	return ctx, s
}

// StartWebhookSpan is a convenience wrapper that pre-populates webhook attrs.
func StartWebhookSpan(ctx context.Context, platform, datasource, eventType string) (context.Context, *Span) {
	ctx, span := Start(ctx, "webhook.process")
	span.SetAttr("webhook.platform", platform)
	span.SetAttr("webhook.datasource", datasource)
	span.SetAttr("webhook.event_type", eventType)
	return ctx, span
}

// StartOrchestratorSpan is a convenience wrapper for outbound gRPC calls.
func StartOrchestratorSpan(ctx context.Context, rpcMethod string) (context.Context, *Span) {
	ctx, span := Start(ctx, "orchestrator."+rpcMethod)
	span.SetAttr("rpc.system", "grpc")
	span.SetAttr("rpc.method", rpcMethod)
	span.SetAttr("rpc.service", "vartrack.v1.Orchestrator")
	return ctx, span
}

func asString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return slog.AnyValue(v).String()
}
