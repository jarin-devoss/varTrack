// Package monitoring – wiring guide
//
// This file is NOT compiled (build tag: ignore).
// It serves as a self-contained, reviewable reference for integrating the
// monitoring package into the gateway service.
//
// To actually apply these changes, merge the snippets below into the
// corresponding source files.

//go:build ignore

package monitoring

/*
════════════════════════════════════════════════════════════════════════════════
1.  cmd/main.go  –  Add monitoring.Init after the gRPC connection is established
════════════════════════════════════════════════════════════════════════════════

	import "gateway-service/internal/monitoring"

	// After grpc.NewClient(…) in run():
	m := monitoring.Init(ctx, monitoring.BuildInfo{
		Version:   version,   // pass via -ldflags="-X main.version=$(git describe)"
		Commit:    commit,
		GoVersion: runtime.Version(),
	}, conn)

	// Expose /metrics on the admin server (already started on :9090).
	// Add this route inside NewAdminServer or modify AdminConfig:
	//   mux.Handle("/metrics", m.Handler())


════════════════════════════════════════════════════════════════════════════════
2.  internal/router.go  –  Instrument the mux with the metrics middleware
════════════════════════════════════════════════════════════════════════════════

	import "gateway-service/internal/monitoring"

	// In NewRouter, after defaults are set:
	m := monitoring.DefaultMetrics()

	// Replace the plain middleware wrapping in setupRoutes with instrumented ones:
	instrumentedLimiter    := monitoring.NewInstrumentedRateLimiter(r.limiter, m)
	instrumentedKeyedLimiter := monitoring.NewInstrumentedKeyedRateLimiter(r.keyedLimiter, m)

	r.mux.Handle("/webhooks/", http.StripPrefix("/webhooks",
		instrumentedLimiter.Middleware(
			instrumentedKeyedLimiter.Middleware(
				routes.WebhookRoutes(r.bundleService, r.grpcClient, r.instrumentedBreaker),
			),
		),
	))

	// Wrap the entire mux with the HTTP instrumentation middleware:
	// In buildMiddlewareChain, after Recovery():
	h = monitoring.InstrumentHandler(h, m)


════════════════════════════════════════════════════════════════════════════════
3.  internal/handlers/webhooks.go  –  Record per-webhook metrics
════════════════════════════════════════════════════════════════════════════════

	import (
		"gateway-service/internal/monitoring"
		"time"
	)

	// In WebhookHandler.Handle, replace the current success/error paths with:

	start := time.Now()
	ctx, span := monitoring.StartWebhookSpan(r.Context(), platformName, datasourceName, eventType)
	defer func() { span.End(webhookErr) }()
	m := monitoring.DefaultMetrics()

	// After a successful orchestrator call:
	m.IncWebhook(datasourceName, platformName, eventType, monitoring.OutcomeAccepted.String())
	m.ObserveWebhookProcessing(datasourceName, platformName, eventType, time.Since(start).Seconds())
	m.ObserveWebhookPayload(platformName, float64(len(body)))
	m.IncOrchestratorRequest("ProcessWebhook", "ok")
	m.ObserveOrchestratorDuration("ProcessWebhook", time.Since(start).Seconds())

	// On orchestrator failure:
	m.IncWebhook(datasourceName, platformName, eventType, monitoring.OutcomeOrchestratorError.String())
	m.IncOrchestratorRequest("ProcessWebhook", "error")

	// On circuit breaker rejection:
	m.IncWebhook(datasourceName, platformName, eventType, monitoring.OutcomeCircuitOpen.String())
	m.IncCircuitBreakerOpenRejection()

	// On ignored event:
	m.IncWebhook(datasourceName, platformName, eventType, monitoring.OutcomeIgnored.String())

	// On invalid signature:
	m.IncWebhook(datasourceName, platformName, eventType, monitoring.OutcomeInvalidSignature.String())

	// On invalid JSON:
	m.IncWebhook(datasourceName, platformName, eventType, monitoring.OutcomeInvalidJSON.String())

	// On datasource not found:
	m.IncWebhook(datasourceName, platformName, eventType, monitoring.OutcomeDatasourceNotFound.String())


════════════════════════════════════════════════════════════════════════════════
4.  internal/admin.go  –  Mount /metrics on the admin server
════════════════════════════════════════════════════════════════════════════════

	import "gateway-service/internal/monitoring"

	// In NewAdminServer, alongside the health endpoints:
	mux.Handle("/metrics", monitoring.DefaultMetrics().Handler())


════════════════════════════════════════════════════════════════════════════════
5.  internal/secrets/cache.go  –  Emit cache hit/miss/error metrics
════════════════════════════════════════════════════════════════════════════════

	import "gateway-service/internal/monitoring"

	// In CachingRefResolver.Resolve:

	m := monitoring.DefaultMetrics()

	// Cache hit:
	if ok && time.Now().Before(entry.expiresAt) {
		m.IncSecretResolution(managerName, "hit")
		return entry.value, nil
	}

	// After successful slow-path resolution:
	m.IncSecretResolution(managerName, "miss")
	m.SetSecretCacheSize(float64(len(c.entries)))

	// On error:
	m.IncSecretResolution(managerName, "error")


════════════════════════════════════════════════════════════════════════════════
6.  go.mod  –  Add prometheus dependency (if not already present)
════════════════════════════════════════════════════════════════════════════════

	require (
		github.com/prometheus/client_golang v1.22.0
	)

	Note: the internal/models/monitoring/prometheus.go file already uses this
	package, so it may already be present in go.sum. Run:
		go get github.com/prometheus/client_golang@latest
		go mod tidy
*/
