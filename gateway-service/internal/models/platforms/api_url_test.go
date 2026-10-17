package platforms

import (
	"testing"

	pb_gh "gateway-service/internal/gen/proto/go/vartrack/v1/models/platforms"
)

func TestAPIAddressesComeFromTheProtos(t *testing.T) {
	if githubCloudEndpoint != "https://github.com" || githubCloudAPIURL != "https://api.github.com" ||
		githubEnterpriseAPIPath != "/api/v3" || giteaAPIPath != "/api/v1" {
		t.Fatalf("consts not read from the protos: %q %q %q %q",
			githubCloudEndpoint, githubCloudAPIURL, githubEnterpriseAPIPath, giteaAPIPath)
	}
}

func TestGitHubAPIAddress(t *testing.T) {
	for endpoint, want := range map[string]string{
		"":                           "https://api.github.com",
		"https://github.com":         "https://api.github.com",
		"https://github.com/":        "https://api.github.com",
		"https://api.github.com":     "https://api.github.com",
		"https://ghe.example":        "https://ghe.example/api/v3",
		"https://ghe.example/api/v3": "https://ghe.example/api/v3",
		"https://github.company.com": "https://github.company.com/api/v3", // Enterprise, not github.com
	} {
		g := &GitHub{config: &pb_gh.GitHub{Endpoint: endpoint}}
		if got := g.getBaseAPIURL(); got != want {
			t.Errorf("endpoint %q: API at %q, want %q", endpoint, got, want)
		}
	}
}

func TestGiteaAPIAddress(t *testing.T) {
	g := &Gitea{config: &pb_gh.Gitea{Endpoint: "http://gitea:3000/"}}
	if got := g.getBaseAPIURL(); got != "http://gitea:3000/api/v1" {
		t.Errorf("API at %q", got)
	}
}
