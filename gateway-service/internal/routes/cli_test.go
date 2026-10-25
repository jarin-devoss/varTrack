package routes_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/timestamppb"

	"gateway-service/internal/auth"
	enums "vartrack/libs/gen/vartrack/v1/enums"
	pbm "vartrack/libs/gen/vartrack/v1/models"
	authpb "vartrack/libs/gen/vartrack/v1/models/auth"
	pb "vartrack/libs/gen/vartrack/v1/services"
	"gateway-service/internal/handlers"
	"gateway-service/internal/models"
	"gateway-service/internal/routes"
)

// fakeCLIService records the requests that made it past auth.
type fakeCLIService struct {
	pb.CLIServiceClient
	synced    []*pb.SyncFileRequest
	validated int
	user      string
}

func (f *fakeCLIService) SyncFile(ctx context.Context, in *pb.SyncFileRequest, _ ...grpc.CallOption) (*pb.SyncFileResponse, error) {
	f.synced = append(f.synced, in)
	if md, ok := metadata.FromOutgoingContext(ctx); ok && len(md.Get("x-cli-user")) > 0 {
		f.user = md.Get("x-cli-user")[0]
	}
	return &pb.SyncFileResponse{TaskId: "task-1", Message: "sync task enqueued", DryRun: in.GetDryRun()}, nil
}

func (f *fakeCLIService) ValidateFile(context.Context, *pb.ValidateFileRequest, ...grpc.CallOption) (*pb.ValidateFileResponse, error) {
	f.validated++
	return &pb.ValidateFileResponse{Status: "ok", KeyCount: 1}, nil
}

func (f *fakeCLIService) GetTask(_ context.Context, in *pb.GetCliTaskRequest, _ ...grpc.CallOption) (*pb.CliTask, error) {
	return &pb.CliTask{
		TaskId: in.GetTaskId(), Datasource: "mongo", State: enums.TaskState_TASK_STATE_SUCCEEDED, Written: 19,
		CreatedAt: timestamppb.New(time.Date(2026, 10, 18, 9, 0, 0, 0, time.UTC)),
	}, nil
}

func (f *fakeCLIService) ListTasks(_ context.Context, in *pb.ListCliTasksRequest, _ ...grpc.CallOption) (*pb.ListCliTasksResponse, error) {
	return &pb.ListCliTasksResponse{Tasks: []*pb.CliTask{{TaskId: "task-1", Datasource: "mongo", TenantId: in.GetTenantId(), State: enums.TaskState_TASK_STATE_RUNNING}}}, nil
}

// fakeGit is a Git platform: a token is its holder's login, perms what
// each may do in acme/configs.
type fakeGit struct {
	models.Platform
	perms map[string]models.RepoPermission
}

func (f fakeGit) CallerLogin(_ context.Context, token string) (string, error) {
	if _, ok := f.perms[token]; !ok {
		return "", models.ErrTokenRejected
	}
	return token, nil
}

func (f fakeGit) CallerPermission(_ context.Context, token, repo string) (models.RepoPermission, error) {
	if repo != "acme/configs" {
		return models.RepoPermission{}, nil
	}
	return f.perms[token], nil
}

func (f fakeGit) CallerRepos(context.Context, string) (map[string]models.RepoPermission, error) {
	return nil, nil
}

// CallerCanPush: main is protected, so only repository admins push to it.
func (f fakeGit) CallerCanPush(_ context.Context, token, _, branch string) (bool, error) {
	if branch == "main" {
		return f.perms[token].Admin, nil
	}
	return f.perms[token].Write, nil
}

type oneRule struct{ git fakeGit }

func (o oneRule) RulesForDatasource(ds string) []*pbm.Rule {
	rule := func(platform string) *pbm.Rule {
		return &pbm.Rule{Platform: platform, Datasource: ds, Repositories: []string{"acme/configs"},
			BranchMap: map[string]string{"main": "production", "develop": "staging"}}
	}
	switch ds {
	case "mongo":
		return []*pbm.Rule{rule("gitea")}
	case "multi": // written by a rule on each of two platforms
		return []*pbm.Rule{rule("gitea"), rule("gitea-team")}
	}
	return nil
}

func (o oneRule) FindRule(platform, ds string) *pbm.Rule {
	for _, r := range o.RulesForDatasource(ds) {
		if r.GetPlatform() == platform {
			return r
		}
	}
	return nil
}

func (o oneRule) GetPlatformForRule(context.Context, string, string) (models.Platform, error) {
	return o.git, nil
}
func (o oneRule) GetPlatform(context.Context, string, string) (models.Platform, error) {
	return o.git, nil
}
func (o oneRule) PlatformNames() []string { return []string{"gitea"} }

// TestCLIAuthChain drives /v1/cli/* through Git permissions and OPA as the
// gateway wires them: the caller's Git token, the rule's repositories, the
// example policy.
func TestCLIAuthChain(t *testing.T) {
	policy, err := os.ReadFile(filepath.Join("../../..", "examples/policies/vartrack_authz.rego"))
	if err != nil {
		t.Skipf("example policy not available: %v", err)
	}
	opa, err := auth.NewOPAFromConfig(&authpb.OPAConfig{Source: &authpb.OPAConfig_InlinePolicy{InlinePolicy: string(policy)}})
	if err != nil {
		t.Fatal(err)
	}
	git := fakeGit{perms: map[string]models.RepoPermission{
		"ana":  {Read: true, Write: true, Admin: true}, // repository admin
		"wes":  {Read: true, Write: true},
		"rae":  {Read: true},
		"zed":  {},
		"root": {},
	}}
	authz := auth.NewGitAuthorizer(oneRule{git}, []string{"gitea:root"})
	cli := &fakeCLIService{}
	srv := httptest.NewServer(http.StripPrefix("/v1/cli", routes.CLIRoutes(handlers.NewCLIHandler(cli, authz, opa))))
	defer srv.Close()

	tests := []struct {
		name       string
		token      string
		path       string
		body       string
		wantStatus int
		wantDetail string
	}{
		{"no token", "", "/sync", syncBody("mongo", "staging", false), 401, "Git token is required"},
		{"token the platform rejects", "mallory", "/sync", syncBody("mongo", "staging", false), 401, "rejected"},
		{"unknown datasource", "wes", "/sync", syncBody("nowhere", "staging", false), 404, "no rule"},
		{"two platforms write to it, none named", "wes", "/sync", syncBody("multi", "staging", false), 400, "say which with --platform"},
		{"two platforms write to it, one named", "wes", "/sync", platformBody("multi", "gitea", "staging"), 202, ""},
		{"a platform with no rule for it", "wes", "/sync", platformBody("multi", "github", "staging"), 404, "platform github and datasource multi"},
		{"no access", "zed", "/sync", syncBody("mongo", "staging", true), 403, "needs read access to acme/configs"},
		{"reader syncs", "rae", "/sync", syncBody("mongo", "staging", false), 403, "may not push to develop"},
		{"reader dry-runs production", "rae", "/sync", syncBody("mongo", "production", true), 202, ""},
		{"reader validates", "rae", "/validate", `{"file_path":"configs/app.yaml","content":"a: 1","datasource":"mongo"}`, 200, ""},
		{"no access validates the rule", "zed", "/validate", `{"file_path":"configs/app.yaml","content":"a: 1","datasource":"mongo"}`, 403, "read access"},
		{"anyone known validates a file", "zed", "/validate", `{"file_path":"configs/app.yaml","content":"a: 1"}`, 200, ""},
		{"writer syncs staging", "wes", "/sync", syncBody("mongo", "staging", false), 202, ""},
		{"writer syncs production (protected main)", "wes", "/sync", syncBody("mongo", "production", false), 403, "may not push to main"},
		{"writer syncs staging from a tag", "wes", "/sync", `{"datasource":"mongo","env":"staging","revision":"v1.4.0"}`, 202, ""},
		{"writer rolls production back to a tag", "wes", "/sync", `{"datasource":"mongo","env":"production","revision":"v1.4.0"}`, 403, "may not push to main"},
		{"neither file nor revision", "wes", "/sync", `{"datasource":"mongo","env":"staging"}`, 400, "revision"},
		{"repository admin syncs production", "ana", "/sync", syncBody("mongo", "production", false), 202, ""},
		{"varTrack admin syncs production", "root", "/sync", syncBody("mongo", "production", false), 202, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			before := len(cli.synced) + cli.validated
			req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/cli"+tc.path, strings.NewReader(tc.body))
			req.Header.Set("Content-Type", "application/json")
			if tc.token != "" {
				req.Header.Set("Authorization", "Bearer "+tc.token)
			}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			var body map[string]interface{}
			_ = json.NewDecoder(resp.Body).Decode(&body)

			if resp.StatusCode != tc.wantStatus {
				t.Fatalf("status = %d, want %d (body %v)", resp.StatusCode, tc.wantStatus, body)
			}
			if detail, _ := body["detail"].(string); !strings.Contains(detail, tc.wantDetail) {
				t.Errorf("detail = %q, want it to mention %q", detail, tc.wantDetail)
			}
			forwarded := len(cli.synced)+cli.validated > before
			if wantForward := tc.wantStatus < 300; forwarded != wantForward {
				t.Errorf("forwarded to orchestrator = %v, want %v", forwarded, wantForward)
			}
		})
	}

	if cli.user != "root@gitea" {
		t.Errorf("x-cli-user sent to the orchestrator = %q, want the last caller as login@platform", cli.user)
	}

	get := func(path, token string) (int, map[string]interface{}) {
		req, _ := http.NewRequest(http.MethodGet, srv.URL+"/v1/cli"+path, nil)
		req.Header.Set("Authorization", "Bearer "+token)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var body map[string]interface{}
		_ = json.NewDecoder(resp.Body).Decode(&body)
		return resp.StatusCode, body
	}

	code, task := get("/tasks/task-1", "rae")
	if code != 200 || task["state"] != "SUCCEEDED" || task["written"] != float64(19) || task["created_at"] != "2026-10-18T09:00:00Z" {
		t.Errorf("GET /tasks/task-1 = %d %v", code, task)
	}
	if code, _ := get("/tasks/task-1", "zed"); code != 404 {
		t.Errorf("GET /tasks/task-1 without read access = %d, want 404", code)
	}
	code, list := get("/tasks?tenant_id=acme&limit=5", "rae")
	tasks, _ := list["tasks"].([]interface{})
	if code != 200 || len(tasks) != 1 || tasks[0].(map[string]interface{})["tenant_id"] != "acme" {
		t.Errorf("GET /tasks = %d %v", code, list)
	}
	code, list = get("/tasks?tenant_id=acme&limit=5", "zed")
	if tasks, _ := list["tasks"].([]interface{}); code != 200 || len(tasks) != 0 {
		t.Errorf("GET /tasks without read access = %d %v, want no tasks", code, list)
	}
	if code, _ := get("/tasks/task-1", "mallory"); code != 401 {
		t.Errorf("GET /tasks with a rejected token = %d, want 401", code)
	}
}

func syncBody(datasource, env string, dryRun bool) string {
	b, _ := json.Marshal(map[string]interface{}{
		"datasource": datasource, "env": env, "file_path": "configs/app.yaml",
		"content": "a: 1", "dry_run": dryRun,
	})
	return string(b)
}

func platformBody(datasource, platform, env string) string {
	return `{"datasource":"` + datasource + `","platform":"` + platform + `","env":"` + env +
		`","file_path":"configs/app.yaml","content":"a: 1"}`
}

// The orchestrator is told the platform the permission was checked on, named
// or not, so it syncs the same rule.
func TestTheSyncGoesToTheRuleThePermissionWasCheckedOn(t *testing.T) {
	git := fakeGit{perms: map[string]models.RepoPermission{"wes": {Read: true, Write: true}}}
	authz := auth.NewGitAuthorizer(oneRule{git}, nil)
	cli := &fakeCLIService{}
	srv := httptest.NewServer(http.StripPrefix("/v1/cli", routes.CLIRoutes(handlers.NewCLIHandler(cli, authz, nil))))
	defer srv.Close()

	post := func(body string) int {
		req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/cli/sync", strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer wes")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		return resp.StatusCode
	}
	if status := post(syncBody("mongo", "staging", false)); status != 202 {
		t.Fatalf("status = %d", status)
	}
	if status := post(platformBody("multi", "gitea-team", "staging")); status != 202 {
		t.Fatalf("status = %d", status)
	}
	if len(cli.synced) != 2 || cli.synced[0].GetPlatform() != "gitea" || cli.synced[1].GetPlatform() != "gitea-team" {
		t.Errorf("forwarded platforms = %v, want gitea (the only rule) and gitea-team (named)", cli.synced)
	}
}
