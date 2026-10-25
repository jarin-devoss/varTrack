package handlers_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc"

	pbm "vartrack/libs/gen/vartrack/v1/models"
	pbp "vartrack/libs/gen/vartrack/v1/models/platforms"
	pb "vartrack/libs/gen/vartrack/v1/services"
	"gateway-service/internal/middlewares"
	"gateway-service/internal/models"
	_ "gateway-service/internal/models/platforms" // the github and gitea drivers
	"gateway-service/internal/routes"
)

// recorder is the orchestrator: it keeps the platform each webhook was
// forwarded under.
type recorder struct {
	pb.OrchestratorClient
	forwarded []*pb.ProcessWebhookRequest
}

func (r *recorder) ProcessWebhook(_ context.Context, in *pb.ProcessWebhookRequest, _ ...grpc.CallOption) (*pb.ProcessWebhookResponse, error) {
	r.forwarded = append(r.forwarded, in)
	return &pb.ProcessWebhookResponse{TaskId: "task-1", Message: "queued"}, nil
}

func str(s string) *string { return &s }

// bundleWith is example 15: mongo-primary is written by a rule on GitHub and
// by a rule on a second, Gitea, platform ("gitea-internal"). Each of the
// others only has one platform.
func bundleWith(rules ...*pbm.Rule) *models.Bundle {
	return models.NewBundle(&pbm.Bundle{
		Platforms: []*pbm.Platform{
			{Config: &pbm.Platform_Github{Github: &pbp.GitHub{
				EventTypeHeader: "X-GitHub-Event", GitScmSignature: "X-Hub-Signature-256", PushEventName: "push", PrEventName: "pull_request"}}},
			{Config: &pbm.Platform_Gitea{Gitea: &pbp.Gitea{Tag: str("internal")}}},
		},
		Rules: rules,
	})
}

func rule(platform, datasource string) *pbm.Rule {
	return &pbm.Rule{Platform: platform, Datasource: datasource, Repositories: []string{"acme/configs"}}
}

const push = `{"ref":"refs/heads/main","repository":{"full_name":"acme/configs"}}`

func deliver(t *testing.T, bundle *models.Bundle, datasource string, headers map[string]string) (*recorder, int, map[string]interface{}) {
	t.Helper()
	orch := &recorder{}
	// isProduction=false: no HMAC or replay checks; the platform choice is what is tested.
	h := routes.WebhookRoutes(bundle, orch, middlewares.NewCircuitBreaker(middlewares.DefaultCircuitBreakerConfig()), nil, false)
	srv := httptest.NewServer(http.StripPrefix("/webhooks", h))
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/webhooks/"+datasource, strings.NewReader(push))
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&body)
	return orch, resp.StatusCode, body
}

func TestTheWebhookIsVerifiedAsThePlatformThatSentIt(t *testing.T) {
	both := bundleWith(rule("github", "mongo-primary"), rule("gitea-internal", "mongo-primary"))

	tests := []struct {
		name         string
		headers      map[string]string
		wantStatus   int
		wantPlatform string
	}{
		{"a GitHub delivery", map[string]string{"X-GitHub-Event": "push"}, 202, "github"},
		// Gitea also sends GitHub's header for compatibility: the Gitea one wins.
		{"a Gitea delivery", map[string]string{"X-Gitea-Event": "push", "X-GitHub-Event": "push"}, 202, "gitea-internal"},
		{"a Gitea delivery without the GitHub header", map[string]string{"X-Gitea-Event": "push"}, 202, "gitea-internal"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			orch, status, body := deliver(t, both, "mongo-primary", tc.headers)
			if status != tc.wantStatus {
				t.Fatalf("status = %d, want %d (%v)", status, tc.wantStatus, body)
			}
			if len(orch.forwarded) != 1 || orch.forwarded[0].GetPlatform() != tc.wantPlatform {
				t.Errorf("forwarded %v, want one webhook for platform %q", orch.forwarded, tc.wantPlatform)
			}
		})
	}
}

func TestAWebhookFromNoPlatformWithARuleIsRefusedAndSaysWhich(t *testing.T) {
	both := bundleWith(rule("github", "mongo-primary"), rule("gitea-internal", "mongo-primary"))

	orch, status, body := deliver(t, both, "mongo-primary", map[string]string{"X-Gitlab-Event": "Push Hook"})

	if status != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (%v)", status, body)
	}
	detail, _ := json.Marshal(body)
	for _, want := range []string{"github (X-GitHub-Event)", "gitea-internal (X-Gitea-Event)"} {
		if !strings.Contains(string(detail), want) {
			t.Errorf("response %s should name %q", detail, want)
		}
	}
	if len(orch.forwarded) != 0 {
		t.Error("a webhook no platform sent was forwarded")
	}
}

func TestADatasourceWithOneRuleKeepsWorkingWithoutLookingAtHeaders(t *testing.T) {
	one := bundleWith(rule("github", "redis"), rule("gitea-internal", "mongo-primary"))

	orch, status, body := deliver(t, one, "redis", map[string]string{"X-GitHub-Event": "push"})
	if status != 202 || len(orch.forwarded) != 1 || orch.forwarded[0].GetPlatform() != "github" {
		t.Fatalf("status = %d (%v), forwarded %v", status, body, orch.forwarded)
	}

	// With one rule the platform is the rule's, and the platform checks the headers as before.
	_, status, _ = deliver(t, one, "redis", map[string]string{"X-Gitea-Event": "push"})
	if status != http.StatusBadRequest {
		t.Errorf("a Gitea delivery to a GitHub-only datasource: status = %d, want 400", status)
	}

	_, status, _ = deliver(t, one, "nowhere", map[string]string{"X-GitHub-Event": "push"})
	if status != http.StatusNotFound {
		t.Errorf("unknown datasource: status = %d, want 404", status)
	}
}

func TestTwoPlatformsOfTheSameTypeThatFitAreAmbiguous(t *testing.T) {
	two := models.NewBundle(&pbm.Bundle{
		Platforms: []*pbm.Platform{
			{Config: &pbm.Platform_Github{Github: &pbp.GitHub{Tag: str("public"), EventTypeHeader: "X-GitHub-Event", PushEventName: "push"}}},
			{Config: &pbm.Platform_Github{Github: &pbp.GitHub{Tag: str("enterprise"), EventTypeHeader: "X-GitHub-Event", PushEventName: "push"}}},
		},
		Rules: []*pbm.Rule{rule("github-public", "mongo"), rule("github-enterprise", "mongo")},
	})

	orch, status, body := deliver(t, two, "mongo", map[string]string{"X-GitHub-Event": "push"})

	if status != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (%v)", status, body)
	}
	if detail, _ := json.Marshal(body); !strings.Contains(string(detail), "cannot be told apart") {
		t.Errorf("response %s should say the webhooks cannot be told apart", detail)
	}
	if len(orch.forwarded) != 0 {
		t.Error("an ambiguous webhook was forwarded")
	}
}
