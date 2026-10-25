package handlers

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"gateway-service/internal/auth"
	pb "vartrack/libs/gen/vartrack/v1/services"
	"gateway-service/internal/middlewares"
	"gateway-service/internal/models"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// CLIHandler serves the CLI API (/v1/cli/*) by calling the orchestrator's
// CLIService.
//
// Who may do what is the Git platform's to say: the caller's own Git token is
// asked of the rule's platform (auth.GitAuthorizer) — push access to the
// rule's repositories syncs, read access validates, dry-runs and reads
// tasks. An optional OPA policy then decides with the full request: env,
// datasource, file_path, dry_run.
type CLIHandler struct {
	cli   pb.CLIServiceClient
	authz *auth.GitAuthorizer
	opa   *auth.OPAEvaluator // nil when OPA is not configured
}

// NewCLIHandler creates a CLIHandler backed by the orchestrator's CLIService.
// Pass a non-nil OPAEvaluator to enable fine-grained per-request policy checks.
func NewCLIHandler(cli pb.CLIServiceClient, authz *auth.GitAuthorizer, opa *auth.OPAEvaluator) *CLIHandler {
	return &CLIHandler{cli: cli, authz: authz, opa: opa}
}

// HandleSync handles POST /v1/cli/sync: read access to the rule's
// repositories for a dry run; for a sync, what a direct push to the branch
// feeding the environment needs (auth.GitAuthorizer.MaySync).
func (h *CLIHandler) HandleSync(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Datasource string `json:"datasource"`
		Env        string `json:"env"`
		FilePath   string `json:"file_path"`
		Content    string `json:"content"`
		Format     string `json:"format"`
		TenantID   string `json:"tenant_id"`
		DryRun     bool   `json:"dry_run"`
		Label      string `json:"label"`
		Revision   string `json:"revision"`
		Platform   string `json:"platform"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errDetail("invalid JSON: "+err.Error()))
		return
	}
	if req.Datasource == "" || req.Env == "" || (req.Revision == "" && (req.FilePath == "" || req.Content == "")) {
		writeJSON(w, http.StatusBadRequest, errDetail("datasource, env, and file_path and content (or revision) are required"))
		return
	}

	caller, ok := h.callerFor(w, r, req.Platform, req.Datasource)
	if !ok {
		return
	}
	if req.DryRun && !caller.Permission.Read {
		h.forbidden(w, caller, "read", req.Datasource)
		return
	}
	if !req.DryRun {
		ok, why, err := h.authz.MaySync(r.Context(), middlewares.GitTokenFromContext(r.Context()), req.Datasource, req.Env, caller)
		if err != nil {
			writeAuthFailure(w, err)
			return
		}
		if !ok {
			writeJSON(w, http.StatusForbidden, errDetail("permission denied: "+why))
			return
		}
	}

	tenantID := coalesce(req.TenantID, r.Header.Get("X-Tenant-ID"), "default")

	// OPA — after the body is parsed, so env/datasource/file_path are known.
	// Example: writers blocked from env=production by the Rego policy.
	if !h.allowed(w, r, auth.InputFromRequest(
		caller, "sync", "datasource",
		req.Datasource, req.Env, req.FilePath, tenantID, req.DryRun,
	), "policy denied sync to env="+req.Env+" datasource="+req.Datasource+
		" — try --dry-run") {
		return
	}

	resp, err := h.cli.SyncFile(outgoing(r, caller, tenantID), &pb.SyncFileRequest{
		Datasource: req.Datasource,
		Env:        req.Env,
		FilePath:   req.FilePath,
		Content:    req.Content,
		Format:     req.Format,
		TenantId:   tenantID,
		DryRun:     req.DryRun,
		Label:      req.Label,
		Revision:   req.Revision,
		// The rule the permission was checked on, whether or not the caller named it.
		Platform: caller.Platform,
	})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(grpcMessage(err)))
		return
	}

	writeJSON(w, http.StatusAccepted, map[string]interface{}{
		"task_id": resp.GetTaskId(),
		"message": resp.GetMessage(),
		"dry_run": resp.GetDryRun(),
	})
}

// HandleValidate handles POST /v1/cli/validate: read access to the rule's
// repositories, or — with no datasource named — a token some platform of the
// bundle accepts.
func (h *CLIHandler) HandleValidate(w http.ResponseWriter, r *http.Request) {
	var req struct {
		FilePath   string `json:"file_path"`
		Content    string `json:"content"`
		Format     string `json:"format"`
		Datasource string `json:"datasource"`
		TenantID   string `json:"tenant_id"`
		Env        string `json:"env"`
		Platform   string `json:"platform"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errDetail("invalid JSON: "+err.Error()))
		return
	}
	if req.FilePath == "" || req.Content == "" {
		writeJSON(w, http.StatusBadRequest, errDetail("file_path and content are required"))
		return
	}

	var caller *auth.GitCaller
	if req.Datasource != "" {
		var ok bool
		if caller, ok = h.callerFor(w, r, req.Platform, req.Datasource); !ok {
			return
		}
		if !caller.Permission.Read {
			h.forbidden(w, caller, "read", req.Datasource)
			return
		}
	} else {
		var err error
		if caller, err = h.authz.Authenticate(r.Context(), middlewares.GitTokenFromContext(r.Context())); err != nil {
			writeAuthFailure(w, err)
			return
		}
	}

	tenantID := coalesce(req.TenantID, r.Header.Get("X-Tenant-ID"), "default")

	if !h.allowed(w, r, auth.InputFromRequest(
		caller, "validate", "datasource",
		req.Datasource, req.Env, req.FilePath, tenantID, false,
	), "policy denied validate for datasource="+req.Datasource) {
		return
	}

	resp, err := h.cli.ValidateFile(outgoing(r, caller, tenantID), &pb.ValidateFileRequest{
		FilePath:   req.FilePath,
		Content:    req.Content,
		Format:     req.Format,
		Datasource: req.Datasource,
		TenantId:   tenantID,
		Platform:   platformOf(caller, req.Datasource),
	})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(grpcMessage(err)))
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status":    resp.GetStatus(),
		"messages":  nonNil(resp.GetMessages()),
		"key_count": resp.GetKeyCount(),
	})
}

// HandleValidateConfig handles POST /v1/cli/validate-config: checks a
// vartrack.json or a tenant's bundle.json against what a sync would accept
// (presets, the keys each layer may set, config.cue's rules). It names no
// rule and reads no repository, so any token some platform of the bundle
// accepts is enough, as for a validate that names no datasource.
func (h *CLIHandler) HandleValidateConfig(w http.ResponseWriter, r *http.Request) {
	var req struct {
		FilePath string `json:"file_path"`
		Content  string `json:"content"`
		TenantID string `json:"tenant_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errDetail("invalid JSON: "+err.Error()))
		return
	}
	if req.FilePath == "" || req.Content == "" {
		writeJSON(w, http.StatusBadRequest, errDetail("file_path and content are required"))
		return
	}

	caller, err := h.authz.Authenticate(r.Context(), middlewares.GitTokenFromContext(r.Context()))
	if err != nil {
		writeAuthFailure(w, err)
		return
	}

	tenantID := coalesce(req.TenantID, r.Header.Get("X-Tenant-ID"), "default")

	if !h.allowed(w, r, auth.InputFromRequest(
		caller, "validate", "config",
		"", "", req.FilePath, tenantID, false,
	), "policy denied validate-config for "+req.FilePath) {
		return
	}

	resp, err := h.cli.ValidateConfig(outgoing(r, caller, tenantID), &pb.ValidateConfigRequest{
		FilePath: req.FilePath,
		Content:  req.Content,
		TenantId: tenantID,
	})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(grpcMessage(err)))
		return
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"errors":     nonNil(resp.GetErrors()),
		"warnings":   nonNil(resp.GetWarnings()),
		"migrations": nonNil(resp.GetMigrations()),
	})
}

// HandleGetTask handles GET /v1/cli/tasks/{id}: a task the caller cannot
// read the rule of answers 404, exactly like one that does not exist.
func (h *CLIHandler) HandleGetTask(w http.ResponseWriter, r *http.Request) {
	taskID := strings.TrimPrefix(r.URL.Path, "/tasks/")
	taskID = strings.TrimSuffix(taskID, "/")
	if taskID == "" {
		writeJSON(w, http.StatusBadRequest, errDetail("task_id required in path"))
		return
	}

	task, err := h.cli.GetTask(outgoing(r, nil, ""), &pb.GetCliTaskRequest{TaskId: taskID})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(grpcMessage(err)))
		return
	}
	caller, err := h.authz.ForDatasource(r.Context(), middlewares.GitTokenFromContext(r.Context()), task.GetPlatform(), task.GetDatasource())
	switch {
	case errors.Is(err, models.ErrTokenRejected):
		writeAuthFailure(w, err)
		return
	case err != nil || !caller.Permission.Read:
		writeJSON(w, http.StatusNotFound, errDetail("task "+taskID+" not found"))
		return
	}
	writeJSON(w, http.StatusOK, taskJSON(task))
}

// HandleListTasks handles GET /v1/cli/tasks?tenant_id=&limit=: the tasks of
// the rules the caller can read.
func (h *CLIHandler) HandleListTasks(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	tenantID := coalesce(q.Get("tenant_id"), r.Header.Get("X-Tenant-ID"), "default")
	limit, _ := strconv.Atoi(q.Get("limit"))
	token := middlewares.GitTokenFromContext(r.Context())

	caller, err := h.authz.Authenticate(r.Context(), token)
	if err != nil {
		writeAuthFailure(w, err)
		return
	}
	resp, err := h.cli.ListTasks(outgoing(r, caller, tenantID), &pb.ListCliTasksRequest{
		TenantId: tenantID,
		Limit:    int32(limit),
		Cursor:   q.Get("cursor"),
	})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(grpcMessage(err)))
		return
	}

	readable := map[string]bool{}
	tasks := make([]map[string]interface{}, 0, len(resp.GetTasks()))
	for _, t := range resp.GetTasks() {
		rule := t.GetPlatform() + "/" + t.GetDatasource()
		ok, seen := readable[rule]
		if !seen {
			c, err := h.authz.ForDatasource(r.Context(), token, t.GetPlatform(), t.GetDatasource())
			ok = err == nil && c.Permission.Read
			readable[rule] = ok
		}
		if ok {
			tasks = append(tasks, taskJSON(t))
		}
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"tasks":       tasks,
		"next_cursor": resp.GetNextCursor(),
	})
}

// callerFor asks the Git platform of the rule for platform and datasource who
// the caller is and what they may do; it writes the error response itself
// when that fails.
func (h *CLIHandler) callerFor(w http.ResponseWriter, r *http.Request, platform, datasource string) (*auth.GitCaller, bool) {
	caller, err := h.authz.ForDatasource(r.Context(), middlewares.GitTokenFromContext(r.Context()), platform, datasource)
	var ambiguous *auth.AmbiguousRuleError
	switch {
	case errors.Is(err, auth.ErrNoRule):
		what := "datasource " + datasource
		if platform != "" {
			what = "platform " + platform + " and datasource " + datasource
		}
		writeJSON(w, http.StatusNotFound, errDetail("no rule in config.cue syncs to "+what))
		return nil, false
	case errors.As(err, &ambiguous):
		writeJSON(w, http.StatusBadRequest, errDetail(ambiguous.Error()))
		return nil, false
	case err != nil:
		writeAuthFailure(w, err)
		return nil, false
	}
	return caller, true
}

// platformOf is the platform of the rule a request was authorized on: none
// when it named no datasource, and so no rule.
func platformOf(caller *auth.GitCaller, datasource string) string {
	if datasource == "" {
		return ""
	}
	return caller.Platform
}

func (h *CLIHandler) forbidden(w http.ResponseWriter, caller *auth.GitCaller, need, datasource string) {
	writeJSON(w, http.StatusForbidden, errDetail(
		"permission denied: "+caller.Login+" on "+caller.Platform+" needs "+need+" access to "+
			strings.Join(caller.Repos, ", ")+" (the repositories of the "+datasource+" rule)"))
}

// writeAuthFailure answers 401 for a token the platform rejected, 502 when
// the platform could not be asked.
func writeAuthFailure(w http.ResponseWriter, err error) {
	if errors.Is(err, models.ErrTokenRejected) {
		middlewares.WriteAuthError(w, http.StatusUnauthorized, "Git token rejected: "+err.Error())
		return
	}
	middlewares.WriteAuthError(w, http.StatusBadGateway, "cannot ask the Git platform: "+err.Error())
}

// allowed runs the OPA policy, when one is configured, and writes the
// error response itself when the request may not go on.
func (h *CLIHandler) allowed(w http.ResponseWriter, r *http.Request, input auth.OPAInput, deniedMsg string) bool {
	if h.opa == nil {
		return true
	}
	ok, err := h.opa.Allow(r.Context(), input)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errDetail("policy evaluation error: "+err.Error()))
		return false
	}
	if !ok {
		writeJSON(w, http.StatusForbidden, errDetail(deniedMsg))
		return false
	}
	return true
}

// ── helpers ───────────────────────────────────────────────────────────────────

// outgoing passes the caller to the orchestrator as gRPC metadata:
// x-cli-user ("login@platform") is recorded with the task, x-tenant-id
// names the tenant.
func outgoing(r *http.Request, caller *auth.GitCaller, tenantID string) context.Context {
	user := "unknown"
	if caller != nil {
		user = caller.Name()
	}
	md := metadata.Pairs("x-cli-user", user)
	if tenantID != "" {
		md.Set("x-tenant-id", tenantID)
	}
	return metadata.NewOutgoingContext(r.Context(), md)
}

// taskJSON renders a CliTask the way the vt client reads it: snake_case
// fields, the state without its TASK_STATE_ prefix, RFC 3339 times.
func taskJSON(t *pb.CliTask) map[string]interface{} {
	out := map[string]interface{}{
		"task_id":    t.GetTaskId(),
		"state":      strings.TrimPrefix(t.GetState().String(), "TASK_STATE_"),
		"message":    t.GetMessage(),
		"datasource": t.GetDatasource(),
		"env":        t.GetEnv(),
		"file_path":  t.GetFilePath(),
		"tenant_id":  t.GetTenantId(),
		"dry_run":    t.GetDryRun(),
		"label":      t.GetLabel(),
		"written":    t.GetWritten(),
		"pruned":     t.GetPruned(),
	}
	if t.GetError() != "" {
		out["error"] = t.GetError()
	}
	if ts := rfc3339(t.GetCreatedAt()); ts != "" {
		out["created_at"] = ts
	}
	if ts := rfc3339(t.GetFinishedAt()); ts != "" {
		out["finished_at"] = ts
	}
	return out
}

func rfc3339(ts *timestamppb.Timestamp) string {
	if ts == nil {
		return ""
	}
	return ts.AsTime().UTC().Format(time.RFC3339)
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func coalesce(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

func errDetail(msg string) map[string]string {
	return map[string]string{"detail": msg}
}

// grpcMessage is the orchestrator's own message, without the rpc error prefix.
func grpcMessage(err error) string {
	if st, ok := status.FromError(err); ok {
		return st.Message()
	}
	return err.Error()
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
		case codes.Unimplemented:
			return http.StatusNotImplemented
		}
	}
	return http.StatusInternalServerError
}

func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
