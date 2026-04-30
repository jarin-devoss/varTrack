// Package routes defines HTTP route groupings for the gateway service.
package routes

import (
	"net/http"

	"gateway-service/internal/handlers"
)

// HealthRoutes registers liveness and readiness probe endpoints.
// It accepts a shared *HealthHandler so the router can call
// SetUnavailable() on the same instance during graceful shutdown.
func HealthRoutes(h *handlers.HealthHandler) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /liveness", h.Liveness)
	mux.HandleFunc("GET /readiness", h.Readiness)
	return mux
}
