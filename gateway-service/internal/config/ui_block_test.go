package config

import (
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"
)

func TestTheUIBlockParsesWithItsCredentialShorthand(t *testing.T) {
	// The gateway unmarshals strictly: the ui block must be known to its
	// protos, and its credential shorthand must become SecretRefs — while
	// auth.oidc.client_id stays a plain string.
	in := []byte(`{
		"platforms": [{"gitea": {"endpoint": "http://gitea:3000", "protocol": "http", "token": "t"}}],
		"auth": {"oidc": {"issuer_url": "https://idp.example", "client_id": "cli"}},
		"ui": {"public_url": "http://localhost:5173", "auth": {
			"providers": [
				{"platform": "gitea", "client_id": "abc", "client_secret": {"file": "/run/secrets/gitea"}}
			],
			"session_secret": "key",
			"admins": ["gitea:root"]
		}}
	}`)
	normalized, err := normalizeSecretRefs(in)
	if err != nil {
		t.Fatal(err)
	}
	b := &pb_models.Bundle{}
	if err := protojson.Unmarshal(normalized, b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := b.GetAuth().GetOidc().GetClientId(); got != "cli" {
		t.Errorf("auth.oidc.client_id = %q, want the plain string", got)
	}
	p := b.GetUi().GetAuth().GetProviders()[0]
	if p.GetClientId().GetValue() != "abc" || p.GetClientSecret().GetFile() != "/run/secrets/gitea" {
		t.Errorf("provider = %v", p)
	}
	if b.GetUi().GetAuth().GetSessionSecret().GetValue() != "key" {
		t.Errorf("session_secret = %v", b.GetUi().GetAuth().GetSessionSecret())
	}
}
