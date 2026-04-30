// vercel.go — Vercel environment-variable drift watcher.
//
// VarTrack marks Vercel-managed env vars with two sentinel variables:
//
//	VARTRACK_MANAGED_BY = "vartrack"
//	VARTRACK_META       = '{"app.kubernetes.io/managed-by":"vartrack", ...}'
//
// The watcher lists all env vars on the project, confirms the sentinels are
// present, then fingerprints the values of all non-sentinel vars so drift
// from external edits (direct Vercel dashboard / CLI changes) is detected.
//
// rule_config keys consumed:
//
//	vercel_token      string — Vercel API token
//	vercel_project_id string — project ID or slug
//	vercel_team_id    string — (optional) Vercel team ID
//	vercel_api_url    string — (optional) override for testing
package watcher

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	"watcher-service/internal/config"
	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
	"watcher-service/internal/healer"
)

const _vercelAPIBase = "https://api.vercel.com"

// VercelWatcher watches Vercel project env vars for drift.
type VercelWatcher struct {
	name      string
	token     string
	projectID string
	teamID    string
	apiBase   string
	http      *http.Client
	healer    *healer.Healer
	healOpts  healer.HealRequest
}

// NewVercelWatcher creates a VercelWatcher from the typed datasource config.
func NewVercelWatcher(
	_ context.Context,
	dsCfg *dsv1.VercelConfig,
	rule *models.Rule,
	h *healer.Healer,
) (*VercelWatcher, error) {
	if dsCfg.GetToken().GetValue() == "" {
		return nil, fmt.Errorf("vercel watcher %s: token is required", config.RuleName(rule))
	}
	if dsCfg.GetProjectId() == "" {
		return nil, fmt.Errorf("vercel watcher %s: project_id is required", config.RuleName(rule))
	}

	apiBase := dsCfg.GetApiUrl()
	if apiBase == "" {
		apiBase = _vercelAPIBase
	}

	slog.Info("vercel watcher: configured",
		"watcher", config.RuleName(rule), "project", dsCfg.GetProjectId())

	return &VercelWatcher{
		name:      config.RuleName(rule),
		token:     dsCfg.GetToken().GetValue(),
		projectID: dsCfg.GetProjectId(),
		teamID:    dsCfg.GetTeamId(),
		apiBase:   apiBase,
		http:      &http.Client{Timeout: 30 * time.Second},
		healer:    h,
		healOpts: healer.HealRequest{
			Datasource: rule.GetDatasource(),
			Platform:   rule.GetPlatform(),
		},
	}, nil
}

// Name implements Watcher.
func (w *VercelWatcher) Name() string { return "vercel/" + w.name }

// Snapshot lists Vercel env vars, confirms VarTrack sentinels, and returns
// a fingerprint of all managed variable values.
func (w *VercelWatcher) Snapshot(ctx context.Context) (string, error) {
	envVars, err := w.listEnvVars(ctx)
	if err != nil {
		return "", fmt.Errorf("vercel snapshot %s: %w", w.name, err)
	}

	// Must have the sentinel VARTRACK_MANAGED_BY = "vartrack".
	if v, ok := envVars["VARTRACK_MANAGED_BY"]; !ok || v != ManagedByValue {
		// No VarTrack sentinels — treat as empty (unmanaged project).
		return FingerprintRecords(nil), nil
	}

	// Exclude sentinel keys from the data fingerprint.
	delete(envVars, "VARTRACK_MANAGED_BY")
	delete(envVars, "VARTRACK_META")

	return FingerprintRecords(envVars), nil
}

// Restore implements Watcher.
func (w *VercelWatcher) Restore(ctx context.Context) error {
	slog.Info("vercel watcher: triggering heal", "watcher", w.name)
	return w.healer.Heal(ctx, w.healOpts)
}

// Close is a no-op (HTTP client has no persistent connection).
func (w *VercelWatcher) Close() error { return nil }

// ─── Vercel API ───────────────────────────────────────────────────────────────

type vercelEnvResponse struct {
	Envs []struct {
		Key   string `json:"key"`
		Value string `json:"value"`
	} `json:"envs"`
}

// listEnvVars calls GET /v9/projects/{id}/env and returns key→value.
func (w *VercelWatcher) listEnvVars(ctx context.Context) (map[string]string, error) {
	url := fmt.Sprintf("%s/v9/projects/%s/env", w.apiBase, w.projectID)
	if w.teamID != "" {
		url += "?teamId=" + w.teamID
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+w.token)

	resp, err := w.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %w", url, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("vercel API %s: %s — %s", url, resp.Status, body)
	}

	var out vercelEnvResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}

	result := make(map[string]string, len(out.Envs))
	for _, e := range out.Envs {
		result[e.Key] = e.Value
	}
	return result, nil
}
