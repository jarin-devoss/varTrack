package authz_test

import (
	"bytes"
	"context"
	"log/slog"
	"net"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"gateway-service/internal/authz"
	"vartrack/libs/grants"
	pb "vartrack/libs/gen/vartrack/v1/services"
)

const authzSecret = "an-authz-secret-of-at-least-32-chars"

// serve runs the Authz service of w over an in-memory connection, behind the
// grant check, and returns a client for it.
func serve(t *testing.T, w *world, secret string) pb.AuthzClient {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	srv := authz.NewServer(w.engine, secret)
	gs := grpc.NewServer(srv.ServerOption())
	srv.Register(gs)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return lis.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return pb.NewAuthzClient(conn)
}

func signed(t *testing.T, audience, secret string) context.Context {
	t.Helper()
	m, err := grants.NewMinter(secret, 0)
	if err != nil {
		t.Fatal(err)
	}
	token, err := m.Mint(grants.Claims{Audience: audience, Subject: "orchestrator"})
	if err != nil {
		t.Fatal(err)
	}
	return grants.WithBearer(context.Background(), token)
}

func TestTheServiceAnswersOnlyToASignedGrant(t *testing.T) {
	client := serve(t, newWorld(t, ""), authzSecret)
	req := &pb.IdentifyRequest{Identity: &pb.Identity{Platform: "gitea", Token: "wes"}}

	if _, err := client.Identify(context.Background(), req); status.Code(err) != codes.Unauthenticated {
		t.Errorf("no grant: %v", err)
	}
	if _, err := client.Identify(signed(t, grants.AudienceSync, authzSecret), req); status.Code(err) != codes.Unauthenticated {
		t.Errorf("a grant for another audience: %v", err)
	}
	if _, err := client.Identify(signed(t, grants.AudienceAuthz, "another-secret-another-secret-0000"), req); status.Code(err) != codes.Unauthenticated {
		t.Errorf("a grant signed with another key: %v", err)
	}
	who, err := client.Identify(signed(t, grants.AudienceAuthz, authzSecret), req)
	if err != nil || who.GetLogin() != "wes" || who.GetPlatform() != "gitea" || who.GetAdmin() {
		t.Errorf("a good grant: %+v, %v", who, err)
	}
}

func TestTheServiceAnswersLikeTheEngine(t *testing.T) {
	client := serve(t, newWorld(t, ""), authzSecret)
	ctx := signed(t, grants.AudienceAuthz, authzSecret)
	facts := &pb.RuleFacts{
		Platform: "gitea", Datasource: "tenant-redis", Repositories: []string{"acme/configs"},
		BranchMap: map[string]string{"main": "production", "develop": "staging"},
	}
	id := func(token string) []*pb.Identity { return []*pb.Identity{{Platform: "gitea", Token: token}} }

	d, err := client.Decide(ctx, &pb.DecideRequest{Identities: id("wes"), Action: "sync", Rule: facts, Env: "production", Via: "ui"})
	if err != nil || d.GetAllowed() || d.GetDeniedBy() != "git" || d.GetBranch() != "main" ||
		!strings.Contains(d.GetReason(), "may not push to main") {
		t.Errorf("a writer syncs the protected branch: %+v, %v", d, err)
	}
	d, err = client.Decide(ctx, &pb.DecideRequest{Identities: id("wes"), Action: "sync", Rule: facts, Env: "staging", Via: "ui"})
	if err != nil || !d.GetAllowed() || d.GetBranch() != "develop" || d.GetLogin() != "wes" {
		t.Errorf("a writer syncs an unprotected branch: %+v, %v", d, err)
	}

	list, err := client.ReadableRules(ctx, &pb.ReadableRulesRequest{Identities: id("rae"), Rules: []*pb.RuleFacts{facts,
		{Platform: "gitea", Datasource: "hidden", Repositories: []string{"acme/secret-configs"}}}})
	if err != nil || len(list.GetRules()) != 1 || list.GetRules()[0].GetIndex() != 0 ||
		list.GetRules()[0].GetPermission() != "read" || list.GetRules()[0].GetCanSync() {
		t.Errorf("ReadableRules: %+v, %v", list, err)
	}

	// Errors keep their meaning across the wire.
	if _, err := client.Identify(ctx, &pb.IdentifyRequest{Identity: &pb.Identity{Platform: "gitea", Token: "mallory"}}); status.Code(err) != codes.Unauthenticated {
		t.Errorf("a rejected token: %v", err)
	}
	if _, err := client.Decide(ctx, &pb.DecideRequest{Identities: id("wes"), Action: "read", RuleDatasource: "nowhere"}); status.Code(err) != codes.NotFound {
		t.Errorf("no such rule: %v", err)
	}
	if _, err := client.Identify(ctx, &pb.IdentifyRequest{Identity: &pb.Identity{Token: "wes"}}); status.Code(err) != codes.InvalidArgument {
		t.Errorf("no platform named, two in the bundle: %v", err)
	}
	if _, err := client.Decide(ctx, &pb.DecideRequest{Identities: id("wes"), Action: "read", RuleDatasource: "mongo-primary"}); status.Code(err) != codes.InvalidArgument {
		t.Errorf("a datasource on two platforms, none named: %v", err)
	}
}

func TestForgetOverTheWire(t *testing.T) {
	w := newWorld(t, "")
	client := serve(t, w, "")
	ctx := context.Background()
	ident := &pb.IdentifyRequest{Identity: &pb.Identity{Platform: "gitea", Token: "wes"}}
	for i := 0; i < 2; i++ {
		if _, err := client.Identify(ctx, ident); err != nil {
			t.Fatal(err)
		}
	}
	before := w.gitea.calls.Load()
	if before != 1 {
		t.Fatalf("calls = %d, want the second Identify answered from the cache", before)
	}
	if _, err := client.Forget(ctx, &pb.ForgetRequest{Identities: []*pb.Identity{ident.GetIdentity()}}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Identify(ctx, ident); err != nil {
		t.Fatal(err)
	}
	if w.gitea.calls.Load() != before+1 {
		t.Error("Forget did not drop the cached answer")
	}
}

func TestNoTokenIsLogged(t *testing.T) {
	var logs bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })

	client := serve(t, newWorld(t, ""), authzSecret)
	secretToken := "ghp_super_secret_token_value"
	// Refused for want of a grant, and refused by the platform: both paths log.
	_, _ = client.Identify(context.Background(), &pb.IdentifyRequest{Identity: &pb.Identity{Platform: "gitea", Token: secretToken}})
	_, _ = client.Identify(signed(t, grants.AudienceAuthz, authzSecret), &pb.IdentifyRequest{Identity: &pb.Identity{Platform: "gitea", Token: secretToken}})
	time.Sleep(50 * time.Millisecond)
	if strings.Contains(logs.String(), secretToken) {
		t.Errorf("a platform token reached the log:\n%s", logs.String())
	}
}
