package config

import (
	"encoding/json"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
)

func TestNormalizeSecretRefsWrapsSecretsButNotUsernames(t *testing.T) {
	in := []byte(`{"datasources":[{"linux_server":{"username":"deploy","password":"pw"}},{"s3":{"access_key_id":{"value":"k"}}}]}`)

	var got map[string]any
	if err := json.Unmarshal(normalizeSecretRefs(in), &got); err != nil {
		t.Fatal(err)
	}
	ds := got["datasources"].([]any)
	linux := ds[0].(map[string]any)["linux_server"].(map[string]any)
	if linux["username"] != "deploy" {
		t.Errorf("username = %#v, want the plain string", linux["username"])
	}
	if pw, ok := linux["password"].(map[string]any); !ok || pw["value"] != "pw" {
		t.Errorf("password = %#v, want {value: pw}", linux["password"])
	}
	s3 := ds[1].(map[string]any)["s3"].(map[string]any)
	if ak, ok := s3["access_key_id"].(map[string]any); !ok || ak["value"] != "k" {
		t.Errorf("access_key_id = %#v, want it left as a SecretRef", s3["access_key_id"])
	}
}

func TestTheUIBlockParsesWithItsCredentialShorthand(t *testing.T) {
	in := []byte(`{
		"auth": {"oidc": {"issuer_url": "https://idp.example", "client_id": "cli"}},
		"ui": {"public_url": "http://localhost:5173", "auth": {
			"providers": [
				{"platform": "gitea", "client_id": "abc", "client_secret": {"file": "/run/secrets/gitea"}},
				{"platform": "gitea-team", "client_id": {"file": "/run/id"}, "client_secret": "s3cret", "browser_url": "http://127.0.0.1:3001"}
			],
			"admins": ["gitea:root"]
		}}
	}`)
	b := &models.Bundle{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(normalizeSecretRefs(in), b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := b.GetAuth().GetOidc().GetClientId(); got != "cli" {
		t.Errorf("auth.oidc.client_id = %q, want the plain string", got)
	}
	providers := b.GetUi().GetAuth().GetProviders()
	if len(providers) != 2 {
		t.Fatalf("providers = %d, want 2", len(providers))
	}
	if providers[0].GetClientId().GetValue() != "abc" || providers[0].GetClientSecret().GetFile() != "/run/secrets/gitea" {
		t.Errorf("gitea provider = %v", providers[0])
	}
	if providers[1].GetClientId().GetFile() != "/run/id" || providers[1].GetClientSecret().GetValue() != "s3cret" {
		t.Errorf("gitea-team provider = %v", providers[1])
	}
}

func TestASecretManagersOwnCredentialsStayPlainStrings(t *testing.T) {
	// vault token_auth.token is a string in its proto; wrapping it like a
	// datasource's SecretRef token would make the whole bundle fail to load.
	in := []byte(`{
		"secret_managers": [{"vault": {"endpoint": "http://vault:8200", "mount_point": "secret", "kv_version": 2,
			"token_auth": {"token": "root"}}}],
		"ui": {"auth": {"providers": [{"platform": "gitea", "client_id": "id",
			"client_secret": {"ref": {"path": "ui/gitea", "key": "client_secret"}}}], "secret_manager": "vault"}}
	}`)
	b := &models.Bundle{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(normalizeSecretRefs(in), b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := b.GetSecretManagers()[0].GetVault().GetTokenAuth().GetToken(); got != "root" {
		t.Errorf("vault token = %q", got)
	}
	if ref := b.GetUi().GetAuth().GetProviders()[0].GetClientSecret().GetRef(); ref.GetPath() != "ui/gitea" || ref.GetKey() != "client_secret" {
		t.Errorf("client_secret ref = %v", ref)
	}
}
