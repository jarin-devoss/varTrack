package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"vartrack/libs/grants"
	"watcher-service/internal/healer"
	"watcher-service/internal/watcher"
)

const testSecret = "a-secret-that-is-long-enough-for-the-grants-library"

type fakeChecker struct {
	asked            []string
	results          []watcher.CheckResult
	watched          bool
	platform, source string
}

func (f *fakeChecker) CheckNow(_ context.Context, platform, datasource, tenant string) ([]watcher.CheckResult, bool) {
	f.platform, f.source = platform, datasource
	f.asked = append(f.asked, tenant)
	return f.results, f.watched
}

func grantFor(t *testing.T, audience string) string {
	t.Helper()
	m, err := grants.NewMinter(testSecret, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	g, err := m.Mint(grants.Claims{Audience: audience, Subject: "test"})
	if err != nil {
		t.Fatal(err)
	}
	return g
}

func call(h http.Handler, method, target, authorization string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, target, nil)
	if authorization != "" {
		req.Header.Set("Authorization", authorization)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestCheckNeedsAnAdminGrantWhenTheWatcherHasASecret(t *testing.T) {
	f := &fakeChecker{watched: true}
	h := checkHandler(f, testSecret)
	for name, tc := range map[string]struct {
		auth string
		want int
	}{
		"none":                 {"", http.StatusUnauthorized},
		"not a bearer":         {"Basic abc", http.StatusUnauthorized},
		"garbage":              {"Bearer not.a.grant", http.StatusUnauthorized},
		"a grant for a sync":   {"Bearer " + grantFor(t, grants.AudienceSync), http.StatusUnauthorized},
		"a grant for the heal": {"Bearer " + grantFor(t, grants.AudienceWatch), http.StatusUnauthorized},
		"an admin grant":       {"Bearer " + grantFor(t, grants.AudienceAdmin), http.StatusOK},
	} {
		t.Run(name, func(t *testing.T) {
			f.asked = nil
			rec := call(h, http.MethodPost, "/admin/check?platform=gitea&datasource=mongo", tc.auth)
			if rec.Code != tc.want {
				t.Fatalf("status %d (%s), want %d", rec.Code, rec.Body.String(), tc.want)
			}
			if tc.want != http.StatusOK && len(f.asked) != 0 {
				t.Error("a refused call still made the check")
			}
		})
	}
}

func TestCheckRunsOpenWhenTheWatcherHasNoSecret(t *testing.T) {
	f := &fakeChecker{watched: true, results: []watcher.CheckResult{{Watcher: "gitea/mongo", State: healer.GitStateDrifted, Healed: true}}}
	rec := call(checkHandler(f, ""), http.MethodPost, "/admin/check?platform=gitea&datasource=mongo&tenant=demo", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	if f.platform != "gitea" || f.source != "mongo" || f.asked[0] != "demo" {
		t.Errorf("asked %q/%q for tenant %v", f.platform, f.source, f.asked)
	}
	if !strings.Contains(rec.Body.String(), `"healed":true`) || !strings.Contains(rec.Body.String(), `"watcher":"gitea/mongo"`) {
		t.Errorf("body = %s", rec.Body.String())
	}
}

func TestCheckRefusesWhatItCannotDo(t *testing.T) {
	h := checkHandler(&fakeChecker{watched: false}, "")
	for name, tc := range map[string]struct {
		method, target string
		want           int
	}{
		"a GET":                 {http.MethodGet, "/admin/check?platform=a&datasource=b", http.StatusMethodNotAllowed},
		"no datasource":         {http.MethodPost, "/admin/check?platform=a", http.StatusBadRequest},
		"a rule nobody watches": {http.MethodPost, "/admin/check?platform=a&datasource=b", http.StatusNotFound},
	} {
		t.Run(name, func(t *testing.T) {
			if rec := call(h, tc.method, tc.target, ""); rec.Code != tc.want {
				t.Errorf("status %d (%s), want %d", rec.Code, rec.Body.String(), tc.want)
			}
		})
	}
}
