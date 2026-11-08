package healer

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

	"vartrack/libs/grants"
)

const grantSecret = "0123456789abcdef0123456789abcdef"

func TestSignCallsGivesEveryCallAWatchGrant(t *testing.T) {
	m, err := grants.NewMinter(grantSecret, 0)
	if err != nil {
		t.Fatal(err)
	}
	var seen metadata.MD
	invoker := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, opts ...grpc.CallOption) error {
		seen, _ = metadata.FromOutgoingContext(ctx)
		return nil
	}
	if err := signCalls(m)(context.Background(), "/vartrack.v1.Watcher/ListWatchTargets", nil, nil, nil, invoker); err != nil {
		t.Fatal(err)
	}

	v := seen.Get("authorization")
	if len(v) != 1 || !strings.HasPrefix(v[0], "Bearer ") {
		t.Fatalf("authorization = %v", v)
	}
	parts := strings.Split(strings.TrimPrefix(v[0], "Bearer "), ".")
	mac := hmac.New(sha256.New, []byte(grantSecret))
	mac.Write([]byte(parts[0] + "." + parts[1]))
	if base64.RawURLEncoding.EncodeToString(mac.Sum(nil)) != parts[2] {
		t.Fatal("not signed with the secret")
	}
	raw, _ := base64.RawURLEncoding.DecodeString(parts[1])
	var c map[string]any
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	if c["aud"] != "watch" || c["sub"] != "watcher" || c["iss"] != "vartrack" {
		t.Fatalf("claims = %v", c)
	}
}

func TestSignCallsStopsACallItCannotSign(t *testing.T) {
	called := false
	invoker := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, opts ...grpc.CallOption) error {
		called = true
		return nil
	}
	err := signCalls(&grants.Minter{})(context.Background(), "/x/y", nil, nil, nil, invoker)
	if called || status.Code(err) != codes.Internal {
		t.Fatalf("called=%v err=%v", called, err)
	}
}
