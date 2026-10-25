package routes

import (
	"net/http"
	"strings"

	"gateway-service/internal/handlers"
	"gateway-service/internal/middlewares"
)

// CLIRoutes returns an HTTP handler for all /cli/* endpoints.
// Every route needs the caller's Git token (middlewares.GitToken); the
// handlers ask the rule's Git platform what that caller may do.
func CLIRoutes(h *handlers.CLIHandler) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/sync", middlewares.GitToken(http.HandlerFunc(h.HandleSync)))
	mux.Handle("/validate", middlewares.GitToken(http.HandlerFunc(h.HandleValidate)))
	mux.Handle("/validate-config", middlewares.GitToken(http.HandlerFunc(h.HandleValidateConfig)))
	mux.Handle("/tasks/", middlewares.GitToken(http.HandlerFunc(h.HandleGetTask)))
	mux.Handle("/tasks", middlewares.GitToken(http.HandlerFunc(h.HandleListTasks)))

	// Enforce HTTP method on each route.
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path

		switch {
		case path == "/sync" && r.Method != http.MethodPost,
			path == "/validate" && r.Method != http.MethodPost,
			path == "/validate-config" && r.Method != http.MethodPost,
			strings.HasPrefix(path, "/tasks") && r.Method != http.MethodGet:
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		mux.ServeHTTP(w, r)
	})
}
