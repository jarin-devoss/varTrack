package grantclient

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	pb "vartrack/libs/gen/vartrack/v1/services"
	"vartrack/libs/grants"
)

const secret = "0123456789abcdef0123456789abcdef"

type captured struct {
	md metadata.MD
}

func invoke(t *testing.T, ctx context.Context, req any) (captured, error) {
	t.Helper()
	m, err := grants.NewMinter(secret, 0)
	if err != nil {
		t.Fatal(err)
	}
	var got captured
	invoker := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, opts ...grpc.CallOption) error {
		got.md, _ = metadata.FromOutgoingContext(ctx)
		return nil
	}
	err = UnaryInterceptor(m)(ctx, "/vartrack.v1.CLIService/SyncFile", req, nil, nil, invoker)
	return got, err
}

func claimsOf(t *testing.T, md metadata.MD) map[string]any {
	t.Helper()
	v := md.Get("authorization")
	if len(v) != 1 || !strings.HasPrefix(v[0], "Bearer ") {
		t.Fatalf("authorization = %v", v)
	}
	parts := strings.Split(strings.TrimPrefix(v[0], "Bearer "), ".")
	if len(parts) != 3 {
		t.Fatalf("not a grant: %v", v[0])
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(parts[0] + "." + parts[1]))
	if base64.RawURLEncoding.EncodeToString(mac.Sum(nil)) != parts[2] {
		t.Fatal("the grant is not signed with the shared secret")
	}
	raw, _ := base64.RawURLEncoding.DecodeString(parts[1])
	var claims map[string]any
	if err := json.Unmarshal(raw, &claims); err != nil {
		t.Fatal(err)
	}
	return claims
}

func TestCLICallerAndTenantBecomeClaimsAndTheRawHeadersAreDropped(t *testing.T) {
	ctx := metadata.NewOutgoingContext(context.Background(),
		metadata.Pairs("x-cli-user", "alice@gitea", "x-tenant-id", "demo", "x-correlation-id", "c-1"))
	got, err := invoke(t, ctx, &pb.SyncFileRequest{Datasource: "mongo", Platform: "gitea", TenantId: "demo"})
	if err != nil {
		t.Fatal(err)
	}
	c := claimsOf(t, got.md)
	if c["sub"] != "alice@gitea" || c["tenant"] != "demo" || c["platform"] != "gitea" ||
		c["datasource"] != "mongo" || c["aud"] != "sync" || c["iss"] != "vartrack" {
		t.Fatalf("claims = %v", c)
	}
	if len(got.md.Get("x-cli-user")) != 0 || len(got.md.Get("x-tenant-id")) != 0 {
		t.Fatalf("raw caller headers still sent: %v", got.md)
	}
	if got.md.Get("x-correlation-id")[0] != "c-1" {
		t.Fatal("unrelated metadata must be kept")
	}
}

func TestWebhookCallsAreSignedByTheGatewayForTheirPlatformAndDatasource(t *testing.T) {
	got, err := invoke(t, context.Background(), &pb.ProcessWebhookRequest{Platform: "github", Datasource: "redis"})
	if err != nil {
		t.Fatal(err)
	}
	c := claimsOf(t, got.md)
	if c["sub"] != "gateway" || c["platform"] != "github" || c["datasource"] != "redis" {
		t.Fatalf("claims = %v", c)
	}
	if _, ok := c["tenant"]; ok {
		t.Fatalf("a webhook grant must not invent a tenant: %v", c)
	}
}

func TestTheRequestsTenantIsNeverCopiedIntoTheGrant(t *testing.T) {
	got, _ := invoke(t, context.Background(), &pb.ListCliTasksRequest{TenantId: "acme"})
	if _, ok := claimsOf(t, got.md)["tenant"]; ok {
		t.Fatalf("the grant took a tenant from the request body: %v", claimsOf(t, got.md))
	}
}

func TestACallerCannotSmuggleInItsOwnAuthorization(t *testing.T) {
	ctx := metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer forged"))
	got, _ := invoke(t, ctx, &pb.GetCliTaskRequest{})
	if v := got.md.Get("authorization"); len(v) != 1 || v[0] == "Bearer forged" {
		t.Fatalf("authorization = %v", v)
	}
}

func TestAnEmptyCallerFallsBackToTheGateway(t *testing.T) {
	ctx := metadata.NewOutgoingContext(context.Background(), metadata.Pairs("x-cli-user", ""))
	got, _ := invoke(t, ctx, &pb.GetCliTaskRequest{})
	if claimsOf(t, got.md)["sub"] != "gateway" {
		t.Fatalf("claims = %v", claimsOf(t, got.md))
	}
}

func TestAMintFailureStopsTheCall(t *testing.T) {
	called := false
	invoker := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, opts ...grpc.CallOption) error {
		called = true
		return nil
	}
	// a minter with no secret cannot sign: the call must not go out unsigned
	err := UnaryInterceptor(&grants.Minter{})(context.Background(), "/x/y", &pb.GetCliTaskRequest{}, nil, nil, invoker)
	if called {
		t.Fatal("the call went out unsigned")
	}
	if status.Code(err) != codes.Internal {
		t.Fatalf("err = %v", err)
	}
}
