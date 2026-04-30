package routes

import (
	"net/http"
	"strings"

	"gateway-service/internal/auth"
	"gateway-service/internal/handlers"
	"gateway-service/internal/middlewares"
)

// CLIRoutes returns an HTTP handler for all /cli/* endpoints.
// Every route requires a valid Bearer JWT (JWTAuth) and passes through
// the RBAC enforcer for per-operation access control.
func CLIRoutes(
	h        *handlers.CLIHandler,
	jwtAuth  func(http.Handler) http.Handler,
	enforcer *auth.Enforcer,
) http.Handler {
	mux := http.NewServeMux()

	// POST /sync — requires datasource:sync
	mux.Handle("/sync",
		jwtAuth(
			middlewares.RBACCheck(enforcer, "datasource", "sync")(
				http.HandlerFunc(h.HandleSync),
			),
		),
	)

	// POST /validate — requires datasource:validate
	mux.Handle("/validate",
		jwtAuth(
			middlewares.RBACCheck(enforcer, "datasource", "validate")(
				http.HandlerFunc(h.HandleValidate),
			),
		),
	)

	// GET /tasks/{id} and GET /tasks — requires task:get / task:list
	mux.Handle("/tasks/",
		jwtAuth(
			middlewares.RBACCheck(enforcer, "task", "get")(
				http.HandlerFunc(h.HandleGetTask),
			),
		),
	)
	mux.Handle("/tasks",
		jwtAuth(
			middlewares.RBACCheck(enforcer, "task", "list")(
				http.HandlerFunc(taskListStub),
			),
		),
	)

	// Enforce HTTP method on each route.
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path

		switch {
		case path == "/sync" && r.Method != http.MethodPost,
			path == "/validate" && r.Method != http.MethodPost,
			strings.HasPrefix(path, "/tasks") && r.Method != http.MethodGet:
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		mux.ServeHTTP(w, r)
	})
}

func taskListStub(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusNotImplemented)
	_, _ = w.Write([]byte(`{"detail":"task list not yet implemented"}`))
}
