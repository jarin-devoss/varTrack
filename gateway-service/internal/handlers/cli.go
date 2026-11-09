package handlers

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"gateway-service/internal/authz"
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
// Who may do what is the permission engine's to say (internal/authz), the same
// engine the web UI asks: the caller's own Git token is asked of the rule's
// platform — push access to the branch that feeds an environment syncs, read
// access validates, dry-runs and reads tasks — and an optional OPA policy then
// decides with the full request: env, datasource, file_path, dry_run. The
// handlers only gather the facts, ask, and do what the answer says.
type CLIHandler struct {
	cli    pb.CLIServiceClient
	engine *authz.Engine
}

// NewCLIHandler creates a CLIHandler backed by the orchestrator's CLIService.
func NewCLIHandler(cli pb.CLIServiceClient, engine *authz.Engine) *CLIHandler {
	return &CLIHandler{cli: cli, engine: engine}
}

// tenantName is the shape of a tenant name: letters, digits, "-" and "_", starting with a
// letter or digit, at most 63 characters. Checked here, at the entrance, so a name that is
// not one never reaches a header, a query, a key or a path further in.
var tenantName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$`)

// requestedTenant is the tenant the request asks for: the one named in its body or query,
// else its X-Tenant-ID header, else none (""). It writes the 400 itself when a name is not a
// tenant name. A name a request carries is a request, not a permission: the permission engine
// decides which tenant the caller acts for, and that one, not this, is what goes on.
func requestedTenant(w http.ResponseWriter, r *http.Request, named string) (string, bool) {
	tenant := coalesce(named, r.Header.Get("X-Tenant-ID"))
	if tenant != "" && !tenantName.MatchString(tenant) {
		shown := tenant
		if len(shown) > 40 {
			shown = shown[:40] + "…"
		}
		writeJSON(w, http.StatusBadRequest, errDetail(
			"tenant "+strconv.Quote(shown)+" is not a tenant name: letters, digits, '-' and '_' only, up to 63 characters"))
		return "", false
	}
	return tenant, true
}

// HandleSync handles POST /v1/cli/sync: read access to the rule's
// repositories for a dry run; for a sync, what a direct push to the branch
// feeding the environment needs.
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

	tenantID, ok := requestedTenant(w, r, req.TenantID)
	if !ok {
		return
	}
	action := authz.Sync
	if req.DryRun {
		action = authz.DryRun
	}
	d, ok := h.decide(w, r, req.Platform, authz.Request{
		Action: action, RuleDatasource: req.Datasource, Env: req.Env,
		FilePath: req.FilePath, TenantID: tenantID,
	})
	if !ok {
		return
	}
	tenantID = d.Tenant // the tenant the engine checked, not the one the request named

	resp, err := h.cli.SyncFile(outgoing(r, d.Name(), tenantID), &pb.SyncFileRequest{
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
		Platform: d.Platform,
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
// repositories, or — with no datasource named — a token the platform it is
// for accepts.
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

	tenantID, ok := requestedTenant(w, r, req.TenantID)
	if !ok {
		return
	}
	d, ok := h.decide(w, r, req.Platform, authz.Request{
		Action: authz.Validate, RuleDatasource: req.Datasource, Env: req.Env,
		FilePath: req.FilePath, TenantID: tenantID,
	})
	if !ok {
		return
	}
	tenantID = d.Tenant

	resp, err := h.cli.ValidateFile(outgoing(r, d.Name(), tenantID), &pb.ValidateFileRequest{
		FilePath:   req.FilePath,
		Content:    req.Content,
		Format:     req.Format,
		Datasource: req.Datasource,
		TenantId:   tenantID,
		Platform:   platformOf(d, req.Datasource),
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
// rule and reads no repository, so any token the platform it is for accepts
// is enough, as for a validate that names no datasource.
func (h *CLIHandler) HandleValidateConfig(w http.ResponseWriter, r *http.Request) {
	var req struct {
		FilePath string `json:"file_path"`
		Content  string `json:"content"`
		TenantID string `json:"tenant_id"`
		Platform string `json:"platform"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errDetail("invalid JSON: "+err.Error()))
		return
	}
	if req.FilePath == "" || req.Content == "" {
		writeJSON(w, http.StatusBadRequest, errDetail("file_path and content are required"))
		return
	}

	tenantID, ok := requestedTenant(w, r, req.TenantID)
	if !ok {
		return
	}
	d, ok := h.decide(w, r, req.Platform, authz.Request{
		Action: authz.Validate, Resource: "config", FilePath: req.FilePath, TenantID: tenantID,
	})
	if !ok {
		return
	}
	tenantID = d.Tenant

	resp, err := h.cli.ValidateConfig(outgoing(r, d.Name(), tenantID), &pb.ValidateConfigRequest{
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

	task, err := h.cli.GetTask(outgoing(r, "", ""), &pb.GetCliTaskRequest{TaskId: taskID})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(grpcMessage(err)))
		return
	}
	d, err := h.engine.Decide(r.Context(), authz.Request{
		Identities: h.identities(r, ""), Action: authz.Read, Via: authz.ViaCLI,
		RulePlatform: coalesce(task.GetPlatform(), r.Header.Get("X-Git-Platform")), RuleDatasource: task.GetDatasource(),
	})
	switch {
	case errors.Is(err, models.ErrTokenRejected):
		writeAuthFailure(w, err)
		return
	case err != nil || !d.Allowed:
		writeJSON(w, http.StatusNotFound, errDetail("task "+taskID+" not found"))
		return
	}
	writeJSON(w, http.StatusOK, taskJSON(task))
}

// parseTaskLimit reads the limit query value: empty means 0, which the
// orchestrator takes as its default page size. It reports false for anything
// that is not a whole number that fits an int32.
func parseTaskLimit(s string) (int32, bool) {
	if s == "" {
		return 0, true
	}
	n, err := strconv.ParseInt(s, 10, 32)
	if err != nil || n < 0 {
		return 0, false
	}
	return int32(n), true
}

// HandleListTasks handles GET /v1/cli/tasks?tenant_id=&limit=: the tasks of
// the rules the caller can read. The engine says which rules those are before
// the orchestrator reads a page, and the orchestrator skips every other task
// inside its scan, so a page of `limit` holds `limit` readable tasks.
func (h *CLIHandler) HandleListTasks(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	tenantID, ok := requestedTenant(w, r, q.Get("tenant_id"))
	if !ok {
		return
	}
	limit, ok := parseTaskLimit(q.Get("limit"))
	if !ok {
		writeJSON(w, http.StatusBadRequest, errDetail("limit must be a whole number from 0 to 2147483647"))
		return
	}

	named := coalesce(q.Get("platform"), r.Header.Get("X-Git-Platform"))
	who, err := h.engine.Identify(r.Context(), h.identities(r, named)[0])
	if err != nil {
		h.writeEngineError(w, err, "", "")
		return
	}
	tenantID, err = h.engine.ResolveTenant(r.Context(), h.identities(r, named), tenantID)
	if err != nil {
		h.writeTenantError(w, err)
		return
	}
	rules, err := h.readableRules(r.Context(), who, middlewares.GitTokenFromContext(r.Context()), tenantID)
	if err != nil {
		h.writeEngineError(w, err, "", "")
		return
	}
	resp, err := h.cli.ListTasks(outgoing(r, who.Name(), tenantID), &pb.ListCliTasksRequest{
		TenantId: tenantID,
		Limit:    limit,
		Cursor:   q.Get("cursor"),
		Rules:    rules,
	})
	if err != nil {
		writeJSON(w, grpcHTTPStatus(err), errDetail(grpcMessage(err)))
		return
	}

	// The orchestrator skips the tasks of every other rule inside its scan, so a page is
	// full. This only guards against one that does not know to (an older release, deployed
	// out of step): a task outside the set the caller may read is never shown.
	readable := make(map[string]bool, len(rules))
	for _, key := range rules {
		readable[key] = true
	}
	tasks := make([]map[string]interface{}, 0, len(resp.GetTasks()))
	for _, t := range resp.GetTasks() {
		if readable[t.GetPlatform()+"/"+t.GetDatasource()] || (t.GetPlatform() == "" && readable["/"+t.GetDatasource()]) {
			tasks = append(tasks, taskJSON(t))
		}
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"tasks":       tasks,
		"next_cursor": resp.GetNextCursor(),
	})
}

// readableRules is the "platform/datasource" of every rule of the bundle the
// caller may read. A task recorded before it named its platform is matched by its
// datasource alone ("/datasource"), when only one rule writes there.
func (h *CLIHandler) readableRules(ctx context.Context, who authz.Who, token, tenantID string) ([]string, error) {
	rules := h.engine.BundleRules()
	readable, err := h.engine.ReadableRules(ctx, []authz.Identity{{Platform: who.Platform, Token: token}}, rules, tenantID)
	if err != nil {
		return nil, err
	}
	writers := map[string]int{}
	for _, rule := range rules {
		writers[rule.Datasource]++
	}
	keys := []string{}
	for _, access := range readable.Rules {
		rule := rules[access.Index]
		keys = append(keys, rule.Platform+"/"+rule.Datasource)
		if writers[rule.Datasource] == 1 {
			keys = append(keys, "/"+rule.Datasource)
		}
	}
	return keys, nil
}

// identities is the caller's token, for the platform named (which may be none).
func (h *CLIHandler) identities(r *http.Request, platform string) []authz.Identity {
	return []authz.Identity{{Platform: platform, Token: middlewares.GitTokenFromContext(r.Context())}}
}

// decide asks the engine whether the caller may do req, on the platform named (or
// the platform of the one rule that fits). It writes the response itself when the
// answer is no, or when the question could not be answered.
func (h *CLIHandler) decide(w http.ResponseWriter, r *http.Request, named string, req authz.Request) (*authz.Decision, bool) {
	named = coalesce(named, r.Header.Get("X-Git-Platform"))
	req.Identities = h.identities(r, "")
	req.RulePlatform = named
	req.Via = authz.ViaCLI
	if req.RuleDatasource == "" {
		// No rule: the token is for the platform named, if one is.
		req.Identities = h.identities(r, named)
	}
	d, err := h.engine.Decide(r.Context(), req)
	if err != nil {
		h.writeEngineError(w, err, named, req.RuleDatasource)
		return nil, false
	}
	if !d.Allowed {
		writeJSON(w, http.StatusForbidden, errDetail(d.Reason))
		return nil, false
	}
	if d.Tenant == "" {
		// A question about the caller alone names no rule to judge a tenant by: ask the engine
		// which tenant the caller may act for, across all of them.
		tenant, err := h.engine.ResolveTenant(r.Context(), req.Identities, req.TenantID)
		if err != nil {
			h.writeTenantError(w, err)
			return nil, false
		}
		d.Tenant = tenant
	}
	return d, true
}

// writeEngineError answers a question the engine could not answer.
func (h *CLIHandler) writeEngineError(w http.ResponseWriter, err error, platform, datasource string) {
	var ambiguous *authz.AmbiguousRuleError
	var needPlatform *authz.PlatformRequiredError
	var unknownPlatform *authz.UnknownPlatformError
	var policy *authz.PolicyError
	var ambiguousTenant *authz.AmbiguousTenantError
	switch {
	case errors.As(err, &ambiguousTenant):
		writeJSON(w, http.StatusBadRequest, errDetail(ambiguousTenant.Error()))
	case errors.Is(err, authz.ErrNoRule):
		what := "datasource " + datasource
		if platform != "" {
			what = "platform " + platform + " and datasource " + datasource
		}
		writeJSON(w, http.StatusNotFound, errDetail("no rule in config.cue syncs to "+what))
	case errors.As(err, &ambiguous):
		writeJSON(w, http.StatusBadRequest, errDetail(ambiguous.Error()))
	case errors.As(err, &needPlatform):
		writeJSON(w, http.StatusBadRequest, errDetail(needPlatform.Error()))
	case errors.As(err, &unknownPlatform):
		writeJSON(w, http.StatusBadRequest, errDetail(unknownPlatform.Error()))
	case errors.As(err, &policy):
		writeJSON(w, http.StatusInternalServerError, errDetail(policy.Error()))
	default:
		writeAuthFailure(w, err)
	}
}

// writeTenantError answers a tenant the engine would not resolve: several to choose from is
// the caller's to settle (400); one they may not act for, or a token the platform rejects, is
// not.
func (h *CLIHandler) writeTenantError(w http.ResponseWriter, err error) {
	var ambiguous *authz.AmbiguousTenantError
	switch {
	case errors.As(err, &ambiguous):
		writeJSON(w, http.StatusBadRequest, errDetail(ambiguous.Error()))
	case errors.Is(err, models.ErrTokenRejected):
		writeAuthFailure(w, err)
	default:
		writeJSON(w, http.StatusForbidden, errDetail(deniedPrefix+err.Error()))
	}
}

const deniedPrefix = "permission denied: "

// platformOf is the platform of the rule a request was authorized on: none
// when it named no datasource, and so no rule.
func platformOf(d *authz.Decision, datasource string) string {
	if datasource == "" {
		return ""
	}
	return d.Platform
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

// ── helpers ───────────────────────────────────────────────────────────────────

// outgoing says who is asking, as gRPC metadata: x-cli-user (the caller's
// "login@platform", "unknown" when there is none) is recorded with the task,
// x-tenant-id names the tenant. The orchestrator does not take either header on
// trust: the connection's interceptor (internal/grantclient) turns them into a
// signed grant and drops the raw headers before the call leaves the gateway.
func outgoing(r *http.Request, user, tenantID string) context.Context {
	if user == "" {
		user = "unknown"
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
