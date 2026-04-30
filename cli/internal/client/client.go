// Package client provides an HTTP client for the VarTrack gateway API.
package client

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Client talks to the VarTrack gateway over HTTP/HTTPS.
type Client struct {
	baseURL    string
	token      string
	tenantID   string
	httpClient *http.Client
}

// New returns a configured Client.
func New(baseURL, token, tenantID string, insecure bool) *Client {
	transport := http.DefaultTransport
	if insecure {
		transport = &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		}
	}
	return &Client{
		baseURL:  baseURL,
		token:    token,
		tenantID: tenantID,
		httpClient: &http.Client{
			Timeout:   60 * time.Second,
			Transport: transport,
		},
	}
}

// ── CLI API methods ──────────────────────────────────────────────────────────

// SyncRequest is sent to POST /v1/cli/sync.
type SyncRequest struct {
	Datasource string `json:"datasource"`
	Env        string `json:"env"`
	FilePath   string `json:"file_path"`
	Content    string `json:"content"`
	Format     string `json:"format,omitempty"`
	TenantID   string `json:"tenant_id,omitempty"`
	DryRun     bool   `json:"dry_run,omitempty"`
	Label      string `json:"label,omitempty"`
}

// SyncResponse is returned by POST /v1/cli/sync.
type SyncResponse struct {
	TaskID  string `json:"task_id"`
	Message string `json:"message"`
	DryRun  bool   `json:"dry_run"`
}

// ValidateRequest is sent to POST /v1/cli/validate.
type ValidateRequest struct {
	FilePath   string `json:"file_path"`
	Content    string `json:"content"`
	Format     string `json:"format,omitempty"`
	Datasource string `json:"datasource,omitempty"`
	TenantID   string `json:"tenant_id,omitempty"`
}

// ValidateResponse is returned by POST /v1/cli/validate.
type ValidateResponse struct {
	Status   string   `json:"status"`
	Messages []string `json:"messages"`
	KeyCount int      `json:"key_count"`
}

// TaskResponse represents a single task status record.
type TaskResponse struct {
	TaskID     string `json:"task_id"`
	State      string `json:"state"`
	Message    string `json:"message"`
	Datasource string `json:"datasource"`
	Env        string `json:"env"`
	FilePath   string `json:"file_path"`
	TenantID   string `json:"tenant_id"`
	DryRun     bool   `json:"dry_run"`
	Written    int    `json:"written"`
	Pruned     int    `json:"pruned"`
	Error      string `json:"error,omitempty"`
	CreatedAt  string `json:"created_at,omitempty"`
	FinishedAt string `json:"finished_at,omitempty"`
}

// TaskListResponse wraps a list of tasks.
type TaskListResponse struct {
	Tasks      []TaskResponse `json:"tasks"`
	NextCursor string         `json:"next_cursor,omitempty"`
}

func (c *Client) Sync(ctx context.Context, req SyncRequest) (*SyncResponse, error) {
	if req.TenantID == "" {
		req.TenantID = c.tenantID
	}
	var resp SyncResponse
	if err := c.post(ctx, "/v1/cli/sync", req, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

func (c *Client) Validate(ctx context.Context, req ValidateRequest) (*ValidateResponse, error) {
	if req.TenantID == "" {
		req.TenantID = c.tenantID
	}
	var resp ValidateResponse
	if err := c.post(ctx, "/v1/cli/validate", req, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

func (c *Client) GetTask(ctx context.Context, taskID string) (*TaskResponse, error) {
	var resp TaskResponse
	if err := c.get(ctx, "/v1/cli/tasks/"+taskID, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

func (c *Client) ListTasks(ctx context.Context, tenantID string, limit int) (*TaskListResponse, error) {
	if tenantID == "" {
		tenantID = c.tenantID
	}
	path := fmt.Sprintf("/v1/cli/tasks?tenant_id=%s&limit=%d", tenantID, limit)
	var resp TaskListResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

// ── HTTP helpers ─────────────────────────────────────────────────────────────

func (c *Client) post(ctx context.Context, path string, body, out interface{}) error {
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("marshal request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(data))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	c.setHeaders(req)
	return c.do(req, out)
}

func (c *Client) get(ctx context.Context, path string, out interface{}) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return err
	}
	c.setHeaders(req)
	return c.do(req, out)
}

func (c *Client) setHeaders(req *http.Request) {
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	if c.tenantID != "" {
		req.Header.Set("X-Tenant-ID", c.tenantID)
	}
}

func (c *Client) do(req *http.Request, out interface{}) error {
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("http %s %s: %w", req.Method, req.URL.Path, err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode >= 400 {
		var apiErr struct {
			Detail  string `json:"detail"`
			Message string `json:"message"`
		}
		_ = json.Unmarshal(body, &apiErr)
		msg := apiErr.Detail
		if msg == "" {
			msg = apiErr.Message
		}
		if msg == "" {
			msg = string(body)
		}
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, msg)
	}

	if out != nil {
		if err := json.Unmarshal(body, out); err != nil {
			return fmt.Errorf("decode response: %w", err)
		}
	}
	return nil
}
