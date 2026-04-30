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

// OPAUser is the identity sub-document inside OPAInput.
type OPAUser struct {
	Sub    string   `json:"sub"`
	Email  string   `json:"email"`
	Groups []string `json:"groups"`
}

// OPAEvaluator evaluates a Rego policy against each CLI request.
//
// Two modes:
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
func NewOPAEvaluatorServer(serverURL string) *OPAEvaluator {
	return &OPAEvaluator{serverURL: serverURL}
}

// Reload re-reads the policy file (if configured) and recompiles.
// Safe to call concurrently — writes are serialised by the mutex.
func (e *OPAEvaluator) Reload(ctx context.Context) error {
	if e.filePath != "" {
		data, err := os.ReadFile(e.filePath)
		if err != nil {
			return fmt.Errorf("reload OPA file %s: %w", e.filePath, err)
		}
		e.regoText = string(data)
	}
	return e.build(ctx)
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
	pq, err := rego.New(
		rego.Query("data.vartrack.authz.allow"),
		rego.Module("vartrack_authz.rego", e.regoText),
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

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return false, fmt.Errorf("OPA server: %w", err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return false, err
	}

	var result struct {
		Result interface{} `json:"result"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return false, fmt.Errorf("parse OPA response: %w", err)
	}

	switch v := result.Result.(type) {
	case bool:
		return v, nil
	case map[string]interface{}:
		return len(v) > 0, nil
	default:
		return false, nil
	}
}

// ── shared helper ─────────────────────────────────────────────────────────────

// InputFromRequest builds an OPAInput from the validated JWT claims and
// the parameters extracted from the incoming CLI request.
func InputFromRequest(
	claims *Claims,
	action, resource, datasource, env, filePath, tenantID string,
	dryRun bool,
) OPAInput {
	var user OPAUser
	if claims != nil {
		user = OPAUser{
			Sub:    claims.Subject,
			Email:  claims.Email,
			Groups: claims.Groups,
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
