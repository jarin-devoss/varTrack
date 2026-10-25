package handlers_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"gateway-service/internal/auth"
	"gateway-service/internal/handlers"
	"gateway-service/internal/models"
	"gateway-service/internal/routes"
	pbm "vartrack/libs/gen/vartrack/v1/models"
	pb "vartrack/libs/gen/vartrack/v1/services"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// onePlatform is a RuleSource with one Git platform that knows one token.
type onePlatform struct{ models.Platform }

func (onePlatform) CallerLogin(_ context.Context, token string) (string, error) {
	if token != "good" {
		return "", models.ErrTokenRejected
	}
	return "wes", nil
}

type noRules struct{}

func (noRules) FindRule(_, _ string) *pbm.Rule          { return nil }
func (noRules) RulesForDatasource(_ string) []*pbm.Rule { return nil }
func (noRules) PlatformNames() []string                 { return []string{"gitea"} }
func (noRules) GetPlatform(_ context.Context, _, _ string) (models.Platform, error) {
	return onePlatform{}, nil
}
func (noRules) GetPlatformForRule(_ context.Context, _, _ string) (models.Platform, error) {
	return onePlatform{}, nil
}

// orchestrator records the ValidateConfig call it gets.
type orchestrator struct {
	pb.CLIServiceClient
	req  *pb.ValidateConfigRequest
	user string
}

func (o *orchestrator) ValidateConfig(ctx context.Context, in *pb.ValidateConfigRequest, _ ...grpc.CallOption) (*pb.ValidateConfigResponse, error) {
	o.req = in
	md, _ := metadata.FromOutgoingContext(ctx)
	if v := md.Get("x-cli-user"); len(v) > 0 {
		o.user = v[0]
	}
	return &pb.ValidateConfigResponse{Errors: []string{"vartrack.json: branch_map must be an object"}}, nil
}

func validateConfig(t *testing.T, orch *orchestrator, token, body string) *httptest.ResponseRecorder {
	t.Helper()
	h := routes.CLIRoutes(handlers.NewCLIHandler(orch, auth.NewGitAuthorizer(noRules{}, nil), nil))
	req := httptest.NewRequest(http.MethodPost, "/validate-config", strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestValidateConfigPassesTheFileOnAsTheCaller(t *testing.T) {
	orch := &orchestrator{}
	rec := validateConfig(t, orch, "good", `{"file_path":".gitea/vartrack.json","content":"{}","tenant_id":"acme"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	if orch.req.GetFilePath() != ".gitea/vartrack.json" || orch.req.GetTenantId() != "acme" {
		t.Errorf("orchestrator got %v", orch.req)
	}
	if orch.user != "wes@gitea" {
		t.Errorf("x-cli-user = %q", orch.user)
	}
	// The lists are always arrays, never null.
	for _, want := range []string{`"errors":["vartrack.json: branch_map must be an object"]`, `"warnings":[]`, `"migrations":[]`} {
		if !strings.Contains(rec.Body.String(), want) {
			t.Errorf("response %s lacks %s", rec.Body, want)
		}
	}
}

func TestValidateConfigNeedsAGoodTokenAndAFile(t *testing.T) {
	orch := &orchestrator{}
	if rec := validateConfig(t, orch, "", `{"file_path":"vartrack.json","content":"{}"}`); rec.Code != http.StatusUnauthorized {
		t.Errorf("no token: status %d", rec.Code)
	}
	if rec := validateConfig(t, orch, "bad", `{"file_path":"vartrack.json","content":"{}"}`); rec.Code != http.StatusUnauthorized {
		t.Errorf("rejected token: status %d", rec.Code)
	}
	if rec := validateConfig(t, orch, "good", `{"file_path":"vartrack.json"}`); rec.Code != http.StatusBadRequest {
		t.Errorf("no content: status %d", rec.Code)
	}
	if orch.req != nil {
		t.Error("the orchestrator was called for a request that should have been refused")
	}
}

func TestValidateConfigIsPostOnly(t *testing.T) {
	h := routes.CLIRoutes(handlers.NewCLIHandler(&orchestrator{}, auth.NewGitAuthorizer(noRules{}, nil), nil))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/validate-config", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("status %d", rec.Code)
	}
}
