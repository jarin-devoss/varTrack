package middlewares

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
)

type gitTokenKeyType struct{}

var gitTokenKey = gitTokenKeyType{}

// GitTokenFromContext returns the caller's Git token read by GitToken.
func GitTokenFromContext(ctx context.Context) string {
	t, _ := ctx.Value(gitTokenKey).(string)
	return t
}

// GitToken reads the caller's Git token from "Authorization: Bearer <token>"
// and refuses a request without one (401). Whether the token is good, and
// what it may do, the Git platforms say.
func GitToken(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") || token == "" {
			WriteAuthError(w, http.StatusUnauthorized,
				"a Git token is required: Authorization: Bearer <token for the rule's Git platform>")
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), gitTokenKey, token)))
	})
}

// WriteAuthError writes {"detail": msg} with code.
func WriteAuthError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": msg})
}
