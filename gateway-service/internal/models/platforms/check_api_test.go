package platforms

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pb_gh "vartrack/libs/gen/vartrack/v1/models/platforms"
)

func githubAt(t *testing.T, status int) *GitHub {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v3" {
			w.WriteHeader(status)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	t.Cleanup(srv.Close)
	return &GitHub{config: &pb_gh.GitHub{Endpoint: srv.URL}, client: srv.Client()}
}

func TestAGithubPlatformThatServesTheGitHubAPIIsFine(t *testing.T) {
	if err := githubAt(t, http.StatusOK).CheckAPI(context.Background()); err != nil {
		t.Errorf("a GitHub-style API was refused: %v", err)
	}
}

func TestAGithubPlatformPointedAtGiteaIsNamedAsSuch(t *testing.T) {
	// Gitea has no /api/v3: the platform answers webhooks but rejects every caller token.
	err := githubAt(t, http.StatusNotFound).CheckAPI(context.Background())
	if err == nil || !strings.Contains(err.Error(), "declare the platform as `gitea`") {
		t.Errorf("err = %v, want it to say the platform should be gitea", err)
	}
}
