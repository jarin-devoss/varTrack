// Package monitoring – Duration helper adapters for monitoring backends.
//
// The proto files now use google.protobuf.Duration instead of raw int32
// millisecond / second fields. These thin wrappers centralise the conversion
// so each backend doesn't repeat protoutil calls inline.

package monitoring

import (
	"time"

	"gateway-service/internal/protoutil"

	monpb "gateway-service/internal/gen/proto/go/vartrack/v1/models/monitoring"
)

// ── Jaeger ────────────────────────────────────────────────────────────────────

// jaegerFlushInterval returns the configured flush interval for the Jaeger
// batch span processor, or the SDK default (5s) when not set.
//
// Before: time.Duration(cfg.GetFlushIntervalMs()) * time.Millisecond
// After:  reads google.protobuf.Duration field directly
func jaegerFlushInterval(cfg *monpb.JaegerConfig) time.Duration {
	return protoutil.DurationOrDefault(cfg.GetFlushInterval(), 5*time.Second)
}

// ── OTel ──────────────────────────────────────────────────────────────────────

// otelBatchTimeout returns the configured trace batch timeout, or SDK default.
//
// Before: time.Duration(cfg.GetBatchTimeoutMs()) * time.Millisecond
func otelBatchTimeout(cfg *monpb.OTelConfig) time.Duration {
	return protoutil.DurationOrDefault(cfg.GetBatchTimeout(), 5*time.Second)
}

// otelMetricsInterval returns the configured metric push interval, or 60s.
//
// Before: time.Duration(cfg.GetMetricsIntervalS()) * time.Second
func otelMetricsInterval(cfg *monpb.OTelConfig) time.Duration {
	return protoutil.DurationOrDefault(cfg.GetMetricsInterval(), 60*time.Second)
}

// ── Prometheus ────────────────────────────────────────────────────────────────

// promPushInterval returns the configured Pushgateway push interval, or 15s.
//
// Before: time.Duration(cfg.GetPushIntervalS()) * time.Second
func promPushInterval(cfg *monpb.PrometheusConfig) time.Duration {
	return protoutil.DurationOrDefault(cfg.GetPushInterval(), 15*time.Second)
}

// ── ELK ───────────────────────────────────────────────────────────────────────

// elkFlushInterval returns the ES bulk flush interval, or 5s.
//
// Before: time.Duration(cfg.GetElasticsearch().GetFlushIntervalS()) * time.Second
func elkFlushInterval(cfg *monpb.ElasticsearchConfig) time.Duration {
	return protoutil.DurationOrDefault(cfg.GetFlushInterval(), 5*time.Second)
}

// logstashTimeout returns the Logstash HTTP client timeout, or 10s.
//
// Before: time.Duration(cfg.GetTimeoutS()) * time.Second
func logstashTimeout(cfg *monpb.LogstashConfig) time.Duration {
	return protoutil.DurationOrDefault(cfg.GetTimeout(), 10*time.Second)
}
