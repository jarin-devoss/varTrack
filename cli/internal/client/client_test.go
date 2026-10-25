package client

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
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
