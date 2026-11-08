package handlers_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"gateway-service/internal/authz"
	"gateway-service/internal/handlers"
	"gateway-service/internal/routes"
)

func TestATenantThatIsNotATenantNameIsRefusedBeforeAnythingIsAsked(t *testing.T) {
	orch := &orchestrator{}
	h := routes.CLIRoutes(handlers.NewCLIHandler(orch, authz.New(noRules{}, nil)))

	for name, req := range map[string]*http.Request{
		"query": httptest.NewRequest(http.MethodGet, "/tasks?tenant_id=demo%26limit%3D1", nil),
		"header": func() *http.Request {
			r := httptest.NewRequest(http.MethodGet, "/tasks", nil)
			r.Header.Set("X-Tenant-ID", "../other")
			return r
		}(),
		"body": func() *http.Request {
			r := httptest.NewRequest(http.MethodPost, "/validate-config",
				strings.NewReader(`{"file_path":"vartrack.json","content":"{}","tenant_id":"a b"}`))
			return r
		}(),
	} {
		req.Header.Set("Authorization", "Bearer good")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "is not a tenant name") {
			t.Errorf("%s: status %d: %s", name, rec.Code, rec.Body)
		}
	}
	if orch.req != nil {
		t.Error("the orchestrator was called for an invalid tenant")
	}
}

func TestAValidTenantNameStillGoesThrough(t *testing.T) {
	orch := &orchestrator{}
	rec := validateConfig(t, orch, "good", `{"file_path":".gitea/vartrack.json","content":"{}","tenant_id":"Acme_Corp-1"}`)
	if rec.Code != http.StatusOK || orch.req.GetTenantId() != "Acme_Corp-1" {
		t.Errorf("status %d: %s", rec.Code, rec.Body)
	}
}
