package platforms

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"gateway-service/internal/models"
)

// A stand-in for the Gitea/GitHub REST API as the holder of "good" sees it.
func fakeAPI(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	auth := func(h http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "token good" {
				http.Error(w, "bad credentials", http.StatusUnauthorized)
				return
			}
			h(w, r)
		}
	}
	mux.HandleFunc("/api/v1/user", auth(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"login":"wes"}`)
	}))
	mux.HandleFunc("/api/v1/repos/acme/configs", auth(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"full_name":"acme/configs","permissions":{"admin":false,"push":true,"pull":true}}`)
	}))
	mux.HandleFunc("/api/v1/repos/acme/public", auth(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"full_name":"acme/public"}`)
	}))
	mux.HandleFunc("/api/v1/user/repos", auth(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("page") == "2" {
			fmt.Fprint(w, `[{"full_name":"acme/web-app","permissions":{"pull":true}}]`)
			return
		}
		if r.URL.Query().Get("limit") != "50" {
			t.Errorf("page size param: %s", r.URL.RawQuery)
		}
		w.Header().Set("Link", fmt.Sprintf(`<http://%s/api/v1/user/repos?limit=50&page=2>; rel="next"`, r.Host))
		fmt.Fprint(w, `[{"full_name":"acme/configs","permissions":{"admin":true,"push":true,"pull":true}}]`)
	}))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func TestCallerAPI(t *testing.T) {
	srv := fakeAPI(t)
	c := callerAPI{client: srv.Client(), base: srv.URL + "/api/v1", scheme: "token", pageParam: "limit"}
	ctx := context.Background()

	if login, err := c.Login(ctx, "good"); err != nil || login != "wes" {
		t.Errorf("Login = %q, %v", login, err)
	}
	if _, err := c.Login(ctx, "bad"); !errors.Is(err, models.ErrTokenRejected) {
		t.Errorf("a rejected token: err = %v", err)
	}
	if _, err := c.Login(ctx, ""); !errors.Is(err, models.ErrTokenRejected) {
		t.Errorf("no token: err = %v", err)
	}

	for repo, want := range map[string]models.RepoPermission{
		"acme/configs": {Read: true, Write: true},
		"acme/public":  {Read: true},
		"acme/hidden":  {}, // 404: missing, or hidden from the caller
	} {
		if got, err := c.Permission(ctx, "good", repo); err != nil || got != want {
			t.Errorf("Permission(%s) = %+v, %v; want %+v", repo, got, err, want)
		}
	}

	repos, err := c.Repos(ctx, "good")
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]models.RepoPermission{
		"acme/configs": {Read: true, Write: true, Admin: true},
		"acme/web-app": {Read: true},
	}
	if len(repos) != len(want) || repos["acme/configs"] != want["acme/configs"] || repos["acme/web-app"] != want["acme/web-app"] {
		t.Errorf("Repos = %v, want %v (both pages)", repos, want)
	}
}
