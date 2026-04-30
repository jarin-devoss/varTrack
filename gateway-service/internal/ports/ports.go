// Package ports documents the default port assignments for VarTrack services.
// These are not imported at runtime — each service reads its address from
// the environment (e.g. GATEWAY_ADDR=:5657). The constants here serve as
// a single source of truth for documentation and tooling.
package ports

const (
	// GatewayHTTP is the default port for the gateway HTTP server (webhook ingestion, health checks).
	GatewayHTTP = 5657

	// OrchestratorGRPC is the default port for the orchestrator gRPC service.
	OrchestratorGRPC = 50051

	// AgentGRPC is the default port for agent gRPC services.
	AgentGRPC = 50052

	// HealthChecks is the default port for health check extensions.
	HealthChecks = 13133

	// MetricsHTTP is the default port for Prometheus metrics scraping.
	MetricsHTTP = 9090
)
