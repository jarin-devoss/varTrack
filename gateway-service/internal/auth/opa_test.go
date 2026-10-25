package auth_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"gateway-service/internal/auth"
	authpb "vartrack/libs/gen/vartrack/v1/models/auth"
)

// repoRoot is the varTrack checkout this module lives in.
const repoRoot = "../../.."

func TestExamplePolicyDecisions(t *testing.T) {
	src, err := os.ReadFile(filepath.Join(repoRoot, "examples/policies/vartrack_authz.rego"))
	if err != nil {
		t.Skipf("example policy not available: %v", err)
	}
	e, err := auth.NewOPAEvaluatorEmbedded(string(src))
	if err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name string
		user auth.OPAUser
		in   auth.OPAInput
		want bool
	}{
		{"writer syncs staging", writer, sync("staging", "configs/app.yaml", false), true},
		{"writer syncs production", writer, sync("production", "configs/app.yaml", false), false},
		{"writer syncs prod-eu", writer, sync("prod-eu", "configs/app.yaml", false), false},
		{"writer syncs a secret in staging", writer, sync("staging", "configs/secrets/db.yaml", false), false},
		{"writer dry-runs production", writer, sync("production", "configs/app.yaml", true), true},
		{"reader dry-runs production", reader, sync("production", "configs/app.yaml", true), true},
		{"repository admin syncs production", auth.OPAUser{Login: "ana", Permission: "admin"}, sync("production", "configs/secrets/db.yaml", false), true},
		{"varTrack admin syncs production", auth.OPAUser{Login: "root", Permission: "read", Admin: true}, sync("production", "configs/app.yaml", false), true},
		{"reader syncs staging", reader, sync("staging", "configs/app.yaml", false), false},
		{"validate is left to Git read access", reader, auth.OPAInput{Action: "validate", Resource: "datasource"}, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			tc.in.User = tc.user
			got, err := e.Allow(context.Background(), tc.in)
			if err != nil {
				t.Fatal(err)
			}
			if got != tc.want {
				t.Errorf("allow = %v, want %v for %+v", got, tc.want, tc.in)
			}
		})
	}
}

var (
	writer = auth.OPAUser{Login: "wes", Platform: "gitea", Permission: "write"}
	reader = auth.OPAUser{Login: "rae", Platform: "gitea", Permission: "read"}
)

// TestDocumentedPoliciesCompile compiles every Rego policy the docs and
// examples show, so a snippet that would stop the gateway cannot ship.
func TestDocumentedPoliciesCompile(t *testing.T) {
	regoFence := regexp.MustCompile("(?s)```rego\\n(.*?)```")
	inline := regexp.MustCompile(`(?s)inline_policy:\s*"""\n(.*?)"""`)

	var files []string
	for _, pattern := range []string{"docs/*/*.md", "docs/*.md", "*.md", "cli/*.md", "examples/bundles/*.cue"} {
		m, _ := filepath.Glob(filepath.Join(repoRoot, pattern))
		files = append(files, m...)
	}
	if len(files) == 0 {
		t.Skip("docs not available")
	}

	found := 0
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		text := strings.ReplaceAll(string(b), "\r\n", "\n")
		for _, re := range []*regexp.Regexp{regoFence, inline} {
			for _, m := range re.FindAllStringSubmatch(text, -1) {
				found++
				if _, err := auth.NewOPAEvaluatorEmbedded(m[1]); err != nil {
					t.Errorf("%s: policy does not compile: %v", f, err)
				}
			}
		}
	}
	if found == 0 {
		t.Error("found no policies — did the docs move?")
	}
}

func TestOPAServerMode(t *testing.T) {
	var got map[string]auth.OPAInput
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"result": true}`))
	}))
	defer srv.Close()

	e := auth.NewOPAEvaluatorServer(srv.URL)
	if err := e.Reload(context.Background()); err != nil {
		t.Errorf("Reload in server mode should do nothing, got %v", err)
	}
	ok, err := e.Allow(context.Background(), sync("staging", "configs/app.yaml", false))
	if err != nil || !ok {
		t.Fatalf("Allow = %v, %v", ok, err)
	}
	if got["input"].Env != "staging" {
		t.Errorf("server got input %+v", got["input"])
	}
}

func TestOPAFileReload(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.rego")
	write := func(body string) {
		if err := os.WriteFile(path, []byte("package vartrack.authz\n"+body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("default allow = false\n")
	e, err := auth.NewOPAEvaluatorFromFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !e.ReadsFile() {
		t.Error("ReadsFile() = false for a file policy")
	}
	in := sync("staging", "configs/app.yaml", false)

	write("default allow = true\n")
	if err := e.Reload(context.Background()); err != nil {
		t.Fatal(err)
	}
	if ok, _ := e.Allow(context.Background(), in); !ok {
		t.Error("the reloaded policy should allow")
	}

	write("allow {\n") // does not compile
	if err := e.Reload(context.Background()); err == nil {
		t.Error("want a compile error")
	}
	if ok, _ := e.Allow(context.Background(), in); !ok {
		t.Error("a failed reload must keep the previous policy")
	}
}

func TestNewOPAFromConfig(t *testing.T) {
	inline := func(p string) *authpb.OPAConfig {
		return &authpb.OPAConfig{Source: &authpb.OPAConfig_InlinePolicy{InlinePolicy: p}}
	}

	tests := []struct {
		name    string
		cfg     *authpb.OPAConfig
		wantErr string
		wantOPA bool
	}{
		{"no opa block", nil, "", false},
		{"inline policy", inline("package vartrack.authz\ndefault allow = true"), "", true},
		{"server url", &authpb.OPAConfig{Source: &authpb.OPAConfig_ServerUrl{ServerUrl: "http://opa:8181/v1/data/vartrack/authz/allow"}}, "", true},
		{"policy that does not compile", inline("package vartrack.authz\nallow {"), "inline_policy", false},
		{"missing policy file", &authpb.OPAConfig{Source: &authpb.OPAConfig_PolicyFile{PolicyFile: "/nope.rego"}}, "policy_file", false},
		{"empty opa block", &authpb.OPAConfig{}, "one of", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			opa, err := auth.NewOPAFromConfig(tc.cfg)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("err = %v, want one mentioning %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if (opa != nil) != tc.wantOPA {
				t.Errorf("OPA configured = %v, want %v", opa != nil, tc.wantOPA)
			}
		})
	}
}

func sync(env, file string, dryRun bool) auth.OPAInput {
	return auth.OPAInput{Action: "sync", Resource: "datasource", Datasource: "mongo", Env: env, FilePath: file, DryRun: dryRun}
}

func TestOPAServerOnlyTrueAllows(t *testing.T) {
	tests := []struct {
		name    string
		status  int
		body    string
		want    bool
		wantErr bool
	}{
		{"allow rule true", 200, `{"result": true}`, true, false},
		{"allow rule false", 200, `{"result": false}`, false, false},
		{"package document, allow false", 200, `{"result": {"allow": false, "reason": "x"}}`, false, false},
		{"package document, allow true", 200, `{"result": {"allow": true}}`, true, false},
		{"undefined rule", 200, `{}`, false, false},
		{"server error", 500, `{"code": "internal_error"}`, false, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer srv.Close()
			got, err := auth.NewOPAEvaluatorServer(srv.URL).Allow(context.Background(), sync("staging", "a.yaml", false))
			if (err != nil) != tc.wantErr || got != tc.want {
				t.Errorf("Allow = %v, %v; want %v, error %v", got, err, tc.want, tc.wantErr)
			}
		})
	}
}
