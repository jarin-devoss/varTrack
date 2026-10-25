package internal

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"gateway-service/internal/handlers"
)

func TestAdminOPAReload(t *testing.T) {
	health := handlers.NewHealthHandler(nil, nil)

	var reloadErr error
	calls := 0
	srv := NewAdminServer(AdminConfig{OPAReload: func(context.Context) error {
		calls++
		return reloadErr
	}}, health)

	post := func() *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		srv.server.Handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/admin/opa/reload", nil))
		return rec
	}

	if rec := post(); rec.Code != http.StatusOK || calls != 1 {
		t.Errorf("reload: status %d, calls %d", rec.Code, calls)
	}

	reloadErr = errors.New("rego_parse_error: unexpected eof")
	if rec := post(); rec.Code != http.StatusUnprocessableEntity || !strings.Contains(rec.Body.String(), "rego_parse_error") {
		t.Errorf("failed reload: status %d, body %s", rec.Code, rec.Body)
	}

	rec := httptest.NewRecorder()
	srv.server.Handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/admin/opa/reload", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET: status %d, want 405", rec.Code)
	}
}

func TestAdminOPAReloadAbsentWithoutOPA(t *testing.T) {
	srv := NewAdminServer(AdminConfig{}, handlers.NewHealthHandler(nil, nil))
	rec := httptest.NewRecorder()
	srv.server.Handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/admin/opa/reload", nil))
	if rec.Code != http.StatusNotFound {
		t.Errorf("status %d, want 404 when no OPA policy is configured", rec.Code)
	}
}
