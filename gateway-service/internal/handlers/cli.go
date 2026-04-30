package handlers

import (
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"gateway-service/internal/auth"
	"gateway-service/internal/middlewares"
	pb "gateway-service/internal/gen/proto/go/vartrack/v1/services"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// CLIHandler forwards CLI sync/validate/task requests to the orchestrator.
// It holds an optional OPAEvaluator for context-aware policy checks
// (env-scoped, path-scoped) that run after the body is parsed.
type CLIHandler struct {
	grpc pb.OrchestratorClient
	opa  *auth.OPAEvaluator // nil when OPA is not configured
}

// NewCLIHandler creates a CLIHandler backed by the given orchestrator gRPC client.
// Pass a non-nil OPAEvaluator to enable fine-grained per-request policy checks.
func NewCLIHandler(grpc pb.OrchestratorClient, opa *auth.OPAEvaluator) *CLIHandler {
	return &CLIHandler{grpc: grpc, opa: opa}
}

// HandleSync handles POST /v1/cli/sync.
//
// Auth order:
//  1. JWT validated by upstream JWTAuth middleware (claims in context).
//  2. Casbin RBAC validated by upstream RBACCheck middleware (role:operator can sync).
//  3. OPA evaluated HERE, after body parse, with full context:
//     env, datasource, file_path, dry_run — so env-scoped rules work correctly.
func (h *CLIHandler) HandleSync(w http.ResponseWriter, r *http.Request) {
	claims := middlewares.ClaimsFromContext(r.Context())

	var req struct {
		Datasource string `json:"datasource"`
		Env        string `json:"env"`
		FilePath   string `json:"file_path"`
		Content    string `json:"content"`
		Format     string `json:"format"`
		TenantID   string `json:"tenant_id"`
		DryRun     bool   `json:"dry_run"`
		Label      string `json:"label"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errDetail("invalid JSON: "+err.Error()))
		return
	}
	if req.Datasource == "" || req.Env == "" || req.FilePath == "" || req.Content == "" {
		writeJSON(w, http.StatusBadRequest, errDetail("datasource, env, file_path, and content are required"))
		return
	}

	tenantID := coalesce(req.TenantID, r.Header.Get("X-Tenant-ID"), "default")

	// OPA check — runs after body parse so env/datasource/file_path are known.
	// This is where "who can sync to which environment" is enforced.
	// Example: role:operator blocked from env=production by Rego policy.
	if h.opa != nil {
		input := auth.InputFromRequest(
			claims, "sync", "datasource",
			req.Datasource, req.Env, req.FilePath, tenantID, req.DryRun,
		)
		allowed, err := h.opa.Allow(r.Context(), input)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, errDetail("policy evaluation error: "+err.Error()))
			return
		}
		if !allowed {
			writeJSON(w, http.StatusForbidden, errDetail(
				"policy denied sync to env="+req.Env+" datasource="+req.Datasource+
					" — check your role or use --dry-run",
			))
			return
		}
	}

	grpcResp, err := h.grpc.ProcessWebhook(r.Context(), &pb.ProcessWebhookRequest{
		Platform:   "cli",
		Datasource: req.Datasource,
		RawPayload: marshalPayload(req),
		Headers: map[string]string{
			"X-Tenant-ID":  tenantID,
			"X-CLI-User":   emailFromClaims(claims),
			"X-CLI-Env":    req.Env,
			"X-CLI-Format": req.Format,
			"X-CLI-DryRun": boolStr(req.DryRun),
			"X-CLI-Label":  req.Label,
		},
		ReceivedAt: timestamppb.New(time.Now()),
	})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(err.Error()))
		return
	}

	writeJSON(w, http.StatusAccepted, map[string]interface{}{
		"task_id": grpcResp.TaskId,
		"message": grpcResp.Message,
		"dry_run": req.DryRun,
	})
}

// HandleValidate handles POST /v1/cli/validate.
// OPA is also applied so policies can restrict which datasources/envs are readable.
func (h *CLIHandler) HandleValidate(w http.ResponseWriter, r *http.Request) {
	claims := middlewares.ClaimsFromContext(r.Context())

	var req struct {
		FilePath   string `json:"file_path"`
		Content    string `json:"content"`
		Format     string `json:"format"`
		Datasource string `json:"datasource"`
		TenantID   string `json:"tenant_id"`
		Env        string `json:"env"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errDetail("invalid JSON: "+err.Error()))
		return
	}
	if req.FilePath == "" || req.Content == "" {
		writeJSON(w, http.StatusBadRequest, errDetail("file_path and content are required"))
		return
	}

	tenantID := coalesce(req.TenantID, r.Header.Get("X-Tenant-ID"), "default")

	if h.opa != nil {
		input := auth.InputFromRequest(
			claims, "validate", "datasource",
			req.Datasource, req.Env, req.FilePath, tenantID, false,
		)
		allowed, err := h.opa.Allow(r.Context(), input)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, errDetail("policy evaluation error: "+err.Error()))
			return
		}
		if !allowed {
			writeJSON(w, http.StatusForbidden, errDetail("policy denied validate for datasource="+req.Datasource))
			return
		}
	}

	grpcResp, err := h.grpc.ProcessWebhook(r.Context(), &pb.ProcessWebhookRequest{
		Platform:   "cli-validate",
		Datasource: coalesce(req.Datasource, "unknown"),
		RawPayload: marshalPayload(req),
		Headers: map[string]string{
			"X-Tenant-ID":    tenantID,
			"X-CLI-Validate": "true",
			"X-CLI-User":     emailFromClaims(claims),
		},
		ReceivedAt: timestamppb.New(time.Now()),
	})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(err.Error()))
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"task_id": grpcResp.TaskId,
		"message": grpcResp.Message,
	})
}

// HandleGetTask handles GET /v1/cli/tasks/{id}.
func (h *CLIHandler) HandleGetTask(w http.ResponseWriter, r *http.Request) {
	taskID := strings.TrimPrefix(r.URL.Path, "/tasks/")
	taskID = strings.TrimSuffix(taskID, "/")
	if taskID == "" {
		writeJSON(w, http.StatusBadRequest, errDetail("task_id required in path"))
		return
	}

	grpcResp, err := h.grpc.GetWebhookTask(r.Context(), &pb.GetWebhookTaskRequest{TaskId: taskID})
	if err != nil {
		if st, ok := status.FromError(err); ok && st.Code() == codes.NotFound {
			writeJSON(w, http.StatusNotFound, errDetail("task not found"))
			return
		}
		writeJSON(w, http.StatusInternalServerError, errDetail(err.Error()))
		return
	}

	writeJSON(w, http.StatusOK, grpcResp)
}

// ── helpers ───────────────────────────────────────────────────────────────────

func marshalPayload(v interface{}) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func emailFromClaims(c *auth.Claims) string {
	if c == nil {
		return "unknown"
	}
	if c.Email != "" {
		return c.Email
	}
	return c.Subject
}

func coalesce(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

func errDetail(msg string) map[string]string {
	return map[string]string{"detail": msg}
}

func grpcHTTPStatus(err error) int {
	if st, ok := status.FromError(err); ok {
		switch st.Code() {
		case codes.InvalidArgument:
			return http.StatusBadRequest
		case codes.NotFound:
			return http.StatusNotFound
		case codes.Unavailable:
			return http.StatusServiceUnavailable
		}
	}
	return http.StatusInternalServerError
}

func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
