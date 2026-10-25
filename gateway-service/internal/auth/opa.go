package auth

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/open-policy-agent/opa/rego"
)

// OPAInput is the document passed to the OPA policy as "input".
// Policies can branch on any of these fields.
type OPAInput struct {
	User       OPAUser `json:"user"`
	Action     string  `json:"action"`     // sync, validate, get, list
	Resource   string  `json:"resource"`   // datasource, task, bundle
	Datasource string  `json:"datasource"` // e.g. "mongo-primary"
	Env        string  `json:"env"`        // e.g. "production"
	FilePath   string  `json:"file_path"`  // e.g. "configs/app.yaml"
	TenantID   string  `json:"tenant_id"`
	DryRun     bool    `json:"dry_run"`
}

// OPAUser is the caller inside OPAInput, as their Git platform knows them.
type OPAUser struct {
	Login        string   `json:"login"`        // their login on the rule's platform
	Platform     string   `json:"platform"`     // e.g. "gitea"
	Permission   string   `json:"permission"`   // on the rule's repositories: admin, write, read, none
	Admin        bool     `json:"admin"`        // in config.cue's ui.auth.admins
	Repositories []string `json:"repositories"` // the rule's repository names and patterns
}

// OPAEvaluator evaluates a Rego policy against each CLI request.
//
// Two modes:
//
//   - Embedded: policy compiled in-process with the OPA Go SDK.
//     Zero network calls, sub-millisecond evaluation after warmup.
//     Use NewOPAEvaluatorEmbedded.
//
//   - Server: policy evaluated by a remote OPA REST server.
//     Flexible policy updates without gateway restart.
//     Use NewOPAEvaluatorServer.
type OPAEvaluator struct {
	// embedded mode
	mu       sync.RWMutex
	prepared *rego.PreparedEvalQuery // nil until first use or explicit Build()

	regoText string // raw Rego source (inline or read from file)
	filePath string // when set, reload from file on Build()

	// server mode
	serverURL string
}

// NewOPAEvaluatorEmbedded compiles the Rego policy in-process.
// The policy text must define package vartrack.authz with a boolean rule "allow".
//
// Example:
//
//	package vartrack.authz
//	default allow = false
//	allow { input.action != "sync" }
//	allow { input.action == "sync"; input.env != "production" }
//	allow { input.action == "sync"; input.env == "production"; "role:admin" == input.user.groups[_] }
//
// The query is compiled once on first call and reused for all subsequent
// requests — thread-safe via a read/write mutex.
func NewOPAEvaluatorEmbedded(regoPolicy string) (*OPAEvaluator, error) {
	e := &OPAEvaluator{regoText: regoPolicy}
	if err := e.build(context.Background()); err != nil {
		return nil, fmt.Errorf("compile OPA policy: %w", err)
	}
	return e, nil
}

// NewOPAEvaluatorFromFile loads a .rego file from disk and compiles it.
// The file is read once at startup; call Reload() to pick up changes.
func NewOPAEvaluatorFromFile(path string) (*OPAEvaluator, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read OPA policy file %s: %w", path, err)
	}
	e := &OPAEvaluator{regoText: string(data), filePath: path}
	if err := e.build(context.Background()); err != nil {
		return nil, fmt.Errorf("compile OPA policy from %s: %w", path, err)
	}
	return e, nil
}

// NewOPAEvaluatorServer creates an evaluator that delegates to a remote OPA
// REST server. serverURL must point to the data API rule endpoint, e.g.:
//
//	http://opa:8181/v1/data/vartrack/authz/allow
//
// Requests to the server time out after five seconds; a policy server that
// does not answer denies the request (the handler answers 500).
func NewOPAEvaluatorServer(serverURL string) *OPAEvaluator {
	return &OPAEvaluator{serverURL: serverURL}
}

// Reload re-reads the policy file (if configured) and recompiles.
// Safe to call concurrently — writes are serialised by the mutex.
// In server mode there is nothing to recompile and Reload does nothing.
// When the new policy does not compile, the previous one stays in force.
func (e *OPAEvaluator) Reload(ctx context.Context) error {
	if e.serverURL != "" {
		return nil
	}
	if e.filePath != "" {
		data, err := os.ReadFile(e.filePath)
		if err != nil {
			return fmt.Errorf("reload OPA file %s: %w", e.filePath, err)
		}
		e.mu.Lock()
		e.regoText = string(data)
		e.mu.Unlock()
	}
	return e.build(ctx)
}

// ReadsFile reports whether the policy comes from a file, the only source
// whose content can change while the gateway runs.
func (e *OPAEvaluator) ReadsFile() bool {
	return e != nil && e.filePath != ""
}

// Allow evaluates the policy against input and returns (true, nil) when
// the request is permitted. Returns (true, nil) when no evaluator is set.
func (e *OPAEvaluator) Allow(ctx context.Context, input OPAInput) (bool, error) {
	if e == nil {
		return true, nil
	}
	if e.serverURL != "" {
		return e.queryServer(ctx, input)
	}
	return e.evalEmbedded(ctx, input)
}

// ── embedded evaluation ───────────────────────────────────────────────────────

// build compiles the Rego source into a PreparedEvalQuery.
// PrepareForEval parses + compiles the module once; subsequent Eval calls
// reuse the compiled plan, making per-request evaluation very fast (~50µs).
func (e *OPAEvaluator) build(ctx context.Context) error {
	e.mu.RLock()
	text := e.regoText
	e.mu.RUnlock()

	pq, err := rego.New(
		rego.Query("data.vartrack.authz.allow"),
		rego.Module("vartrack_authz.rego", text),
	).PrepareForEval(ctx)
	if err != nil {
		return err
	}

	e.mu.Lock()
	e.prepared = &pq
	e.mu.Unlock()
	return nil
}

// evalEmbedded runs the pre-compiled query against the input document.
// The input struct is marshalled to a map so OPA can traverse it as JSON.
func (e *OPAEvaluator) evalEmbedded(ctx context.Context, input OPAInput) (bool, error) {
	e.mu.RLock()
	pq := e.prepared
	e.mu.RUnlock()

	if pq == nil {
		return false, fmt.Errorf("OPA policy not compiled")
	}

	// Convert input struct → map[string]interface{} via JSON round-trip so
	// OPA receives the same field names as the JSON tags.
	raw, err := json.Marshal(input)
	if err != nil {
		return false, fmt.Errorf("marshal OPA input: %w", err)
	}
	var inputMap map[string]interface{}
	if err := json.Unmarshal(raw, &inputMap); err != nil {
		return false, fmt.Errorf("unmarshal OPA input: %w", err)
	}

	results, err := pq.Eval(ctx, rego.EvalInput(inputMap))
	if err != nil {
		return false, fmt.Errorf("OPA eval: %w", err)
	}

	// Empty result set means the "allow" rule was never satisfied → deny.
	if len(results) == 0 || len(results[0].Expressions) == 0 {
		return false, nil
	}

	allowed, ok := results[0].Expressions[0].Value.(bool)
	return ok && allowed, nil
}

// ── server evaluation ─────────────────────────────────────────────────────────

var opaHTTPClient = &http.Client{Timeout: 5 * time.Second}

func (e *OPAEvaluator) queryServer(ctx context.Context, input OPAInput) (bool, error) {
	body, err := json.Marshal(map[string]interface{}{"input": input})
	if err != nil {
		return false, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.serverURL, bytes.NewReader(body))
	if err != nil {
		return false, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := opaHTTPClient.Do(req)
	if err != nil {
		return false, fmt.Errorf("OPA server: %w", err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return false, err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return false, fmt.Errorf("OPA server answered HTTP %d", resp.StatusCode)
	}

	var result struct {
		Result interface{} `json:"result"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return false, fmt.Errorf("parse OPA response: %w", err)
	}

	// Only true allows: the allow rule itself (…/vartrack/authz/allow), or
	// the package document (…/vartrack/authz) with allow set to true.
	switch v := result.Result.(type) {
	case bool:
		return v, nil
	case map[string]interface{}:
		allow, _ := v["allow"].(bool)
		return allow, nil
	default:
		return false, nil
	}
}

// ── shared helper ─────────────────────────────────────────────────────────────

// InputFromRequest builds the OPA input of a CLI request.
func InputFromRequest(
	caller *GitCaller,
	action, resource, datasource, env, filePath, tenantID string,
	dryRun bool,
) OPAInput {
	var user OPAUser
	if caller != nil {
		user = OPAUser{
			Login:        caller.Login,
			Platform:     caller.Platform,
			Permission:   caller.PermissionName(),
			Admin:        caller.Admin,
			Repositories: caller.Repos,
		}
	}
	return OPAInput{
		User:       user,
		Action:     action,
		Resource:   resource,
		Datasource: datasource,
		Env:        strings.ToLower(env),
		FilePath:   filePath,
		TenantID:   tenantID,
		DryRun:     dryRun,
	}
}
