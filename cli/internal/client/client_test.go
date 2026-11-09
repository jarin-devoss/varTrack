package client

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// posted runs call against a gateway that records the JSON body it was sent.
func posted(t *testing.T, call func(c *Client) error) map[string]interface{} {
	t.Helper()
	var body map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"task_id":"t1","status":"ok"}`))
	}))
	defer srv.Close()
	if err := call(New(srv.URL, "token", "", false)); err != nil {
		t.Fatal(err)
	}
	return body
}

func TestSyncNamesThePlatformOnlyWhenGiven(t *testing.T) {
	body := posted(t, func(c *Client) error {
		_, err := c.Sync(context.Background(), SyncRequest{Datasource: "mongo-primary", Env: "staging", Platform: "gitea-internal"})
		return err
	})
	if body["platform"] != "gitea-internal" {
		t.Errorf("platform = %v, want gitea-internal", body["platform"])
	}

	body = posted(t, func(c *Client) error {
		_, err := c.Sync(context.Background(), SyncRequest{Datasource: "mongo", Env: "staging"})
		return err
	})
	if _, sent := body["platform"]; sent {
		t.Errorf("platform sent as %v when none was given", body["platform"])
	}
}

func TestValidateNamesThePlatformOnlyWhenGiven(t *testing.T) {
	body := posted(t, func(c *Client) error {
		_, err := c.Validate(context.Background(), ValidateRequest{FilePath: "a.yaml", Content: "a: 1", Datasource: "mongo-primary", Platform: "github"})
		return err
	})
	if body["platform"] != "github" {
		t.Errorf("platform = %v, want github", body["platform"])
	}
	body = posted(t, func(c *Client) error {
		_, err := c.Validate(context.Background(), ValidateRequest{FilePath: "a.yaml", Content: "a: 1"})
		return err
	})
	if _, sent := body["platform"]; sent {
		t.Error("platform sent when none was given")
	}
}

func TestValidateConfigPostsToItsOwnRouteWithTheTenant(t *testing.T) {
	var path string
	var body map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"errors":["vartrack.json: branch_map must be an object"],"warnings":[],"migrations":null}`))
	}))
	defer srv.Close()

	resp, err := New(srv.URL, "token", "acme", false).ValidateConfig(context.Background(),
		ValidateConfigRequest{FilePath: ".gitea/vartrack.json", Content: "{}"})
	if err != nil {
		t.Fatal(err)
	}
	if path != "/v1/cli/validate-config" {
		t.Errorf("path = %s", path)
	}
	if body["tenant_id"] != "acme" || body["file_path"] != ".gitea/vartrack.json" {
		t.Errorf("body = %v", body)
	}
	if len(resp.Errors) != 1 || len(resp.Migrations) != 0 {
		t.Errorf("resp = %+v", resp)
	}
}

func TestTheClientSaysWhichPlatformTheTokenIsFor(t *testing.T) {
	var got string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Get("X-Git-Platform")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"task_id":"t1","status":"ok"}`))
	}))
	defer srv.Close()

	if _, err := New(srv.URL, "token", "", false).WithPlatform("gitea-team").Sync(context.Background(), SyncRequest{Datasource: "d", Env: "e"}); err != nil {
		t.Fatal(err)
	}
	if got != "gitea-team" {
		t.Errorf("X-Git-Platform = %q, want gitea-team", got)
	}
	if _, err := New(srv.URL, "token", "", false).Sync(context.Background(), SyncRequest{Datasource: "d", Env: "e"}); err != nil {
		t.Fatal(err)
	}
	if got != "" {
		t.Errorf("X-Git-Platform = %q when no platform was given", got)
	}
}

func TestATenantNameCannotAddQueryParameters(t *testing.T) {
	var raw string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"tasks":[]}`))
	}))
	defer srv.Close()
	c := New(srv.URL, "token", "", false)

	// The value that used to become two parameters, tenant_id=demo and limit=1, is refused.
	if _, err := c.ListTasks(context.Background(), "demo&limit=1", 20); err == nil {
		t.Fatal("a tenant name with '&' was sent")
	}
	if raw != "" {
		t.Errorf("a request was made for an invalid tenant: %q", raw)
	}
	if _, err := c.ListTasks(context.Background(), "demo", 20); err != nil {
		t.Fatal(err)
	}
	if raw != "limit=20&tenant_id=demo" {
		t.Errorf("query = %q", raw)
	}
}

func TestInvalidTenantNamesAreRefusedOnEveryCall(t *testing.T) {
	c := New("http://unused", "token", "", false)
	for _, bad := range []string{"a b", "../x", "x?y", "é", strings.Repeat("a", 64), "-lead"} {
		if _, err := c.Sync(context.Background(), SyncRequest{TenantID: bad}); err == nil {
			t.Errorf("Sync accepted tenant %q", bad)
		}
		if _, err := c.Validate(context.Background(), ValidateRequest{TenantID: bad}); err == nil {
			t.Errorf("Validate accepted tenant %q", bad)
		}
		if _, err := c.ValidateConfig(context.Background(), ValidateConfigRequest{TenantID: bad}); err == nil {
			t.Errorf("ValidateConfig accepted tenant %q", bad)
		}
	}
	for _, good := range []string{"", "demo", "Acme_Corp", "team-1", strings.Repeat("a", 63)} {
		if err := CheckTenant(good); err != nil {
			t.Errorf("tenant %q refused: %v", good, err)
		}
	}
}

func TestATaskIdCannotLeaveItsPath(t *testing.T) {
	var path string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.EscapedPath()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	if _, err := New(srv.URL, "t", "", false).GetTask(context.Background(), "a/../b?x=1"); err != nil {
		t.Fatal(err)
	}
	if path != "/v1/cli/tasks/a%2F..%2Fb%3Fx=1" {
		t.Errorf("path = %q", path)
	}
}
