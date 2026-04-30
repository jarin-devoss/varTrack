package middlewares

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"

	"gateway-service/internal/auth"
)

type claimsKeyType struct{}

// claimsKey is the context key for storing validated JWT claims.
var claimsKey = claimsKeyType{}

// ClaimsFromContext retrieves the validated JWT claims from a request context.
// Returns nil when the request did not go through JWTAuth middleware.
func ClaimsFromContext(ctx context.Context) *auth.Claims {
	c, _ := ctx.Value(claimsKey).(*auth.Claims)
	return c
}

// JWTAuth is HTTP middleware that validates the Authorization: Bearer token
// against the provided JWTValidator.  Requests without a valid token receive
// a 401 response.  Validated claims are stored in the request context for
// downstream handlers.
func JWTAuth(v *auth.JWTValidator) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			rawToken := extractBearerToken(r)
			if rawToken == "" {
				writeAuthError(w, http.StatusUnauthorized, "missing Authorization: Bearer token")
				return
			}

			claims, err := v.Validate(r.Context(), rawToken)
			if err != nil {
				writeAuthError(w, http.StatusUnauthorized, "invalid token: "+err.Error())
				return
			}

			ctx := context.WithValue(r.Context(), claimsKey, claims)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// RBACCheck is HTTP middleware that enforces an RBAC policy for a specific
// resource and action.  It must run after JWTAuth (requires claims in context).
func RBACCheck(enforcer *auth.Enforcer, resource, action string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			claims := ClaimsFromContext(r.Context())
			if claims == nil {
				writeAuthError(w, http.StatusUnauthorized, "authentication required")
				return
			}

			if !enforcer.Allow(claims.Subject, claims.Groups, resource, action) {
				writeAuthError(w, http.StatusForbidden,
					"permission denied: "+claims.Subject+" cannot "+action+" "+resource)
				return
			}

			next.ServeHTTP(w, r)
		})
	}
}

// OPACheck is HTTP middleware that evaluates an OPA policy after RBAC.
// It reads contextual fields (datasource, env, file_path) from the context
// values populated by SetOPAContext before calling this middleware.
func OPACheck(evaluator *auth.OPAEvaluator, action, resource string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if evaluator == nil {
				next.ServeHTTP(w, r)
				return
			}

			claims := ClaimsFromContext(r.Context())

			datasource, _ := r.Context().Value(opaCtxDatasource).(string)
			env, _         := r.Context().Value(opaCtxEnv).(string)
			filePath, _    := r.Context().Value(opaCtxFilePath).(string)
			tenantID, _    := r.Context().Value(opaCtxTenantID).(string)
			dryRun, _      := r.Context().Value(opaCtxDryRun).(bool)

			input := auth.InputFromRequest(
				claims, action, resource,
				datasource, env, filePath, tenantID, dryRun,
			)

			allowed, err := evaluator.Allow(r.Context(), input)
			if err != nil {
				writeAuthError(w, http.StatusInternalServerError, "policy evaluation error: "+err.Error())
				return
			}
			if !allowed {
				writeAuthError(w, http.StatusForbidden,
					"request denied by policy: env="+env+" datasource="+datasource)
				return
			}

			next.ServeHTTP(w, r)
		})
	}
}

// OPA context key types — unexported to prevent collisions.
type opaCtxKey string

const (
	opaCtxDatasource opaCtxKey = "opa_datasource"
	opaCtxEnv        opaCtxKey = "opa_env"
	opaCtxFilePath   opaCtxKey = "opa_file_path"
	opaCtxTenantID   opaCtxKey = "opa_tenant_id"
	opaCtxDryRun     opaCtxKey = "opa_dry_run"
)

// SetOPAContext stores per-request values in the context for OPACheck.
// Call this in the handler before delegating to the middleware chain.
func SetOPAContext(ctx context.Context, datasource, env, filePath, tenantID string, dryRun bool) context.Context {
	ctx = context.WithValue(ctx, opaCtxDatasource, datasource)
	ctx = context.WithValue(ctx, opaCtxEnv, env)
	ctx = context.WithValue(ctx, opaCtxFilePath, filePath)
	ctx = context.WithValue(ctx, opaCtxTenantID, tenantID)
	return context.WithValue(ctx, opaCtxDryRun, dryRun)
}

// extractBearerToken parses the Authorization header and returns the raw token.
func extractBearerToken(r *http.Request) string {
	hdr := r.Header.Get("Authorization")
	if !strings.HasPrefix(hdr, "Bearer ") {
		return ""
	}
	return strings.TrimSpace(strings.TrimPrefix(hdr, "Bearer "))
}

func writeAuthError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": msg})
}
