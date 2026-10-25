package bundle

import (
	"encoding/json"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb "vartrack/libs/gen/vartrack/v1/models"
)

// parse is Load after the CUE step: the normalization, then the proto parse.
func parse(t *testing.T, in string, strict bool) *pb.Bundle {
	t.Helper()
	normalized, err := normalizeSecretRefs([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	b := &pb.Bundle{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: !strict}).Unmarshal(normalized, b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return b
}

func TestNormalizeWrapsSecretsButNotUsernames(t *testing.T) {
	normalized, err := normalizeSecretRefs([]byte(
		`{"datasources":[{"linux_server":{"username":"deploy","password":"pw"}},{"s3":{"access_key_id":{"value":"k"}}}]}`))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(normalized, &got); err != nil {
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

func TestSeveralUIProvidersTakeTheShorthandOrTheObjectForm(t *testing.T) {
	b := parse(t, `{
		"auth": {"oidc": {"issuer_url": "https://idp.example", "client_id": "cli"}},
		"ui": {"public_url": "http://localhost:5173", "auth": {
			"providers": [
				{"platform": "gitea", "client_id": "abc", "client_secret": {"file": "/run/secrets/gitea"}},
				{"platform": "gitea-team", "client_id": {"file": "/run/id"}, "client_secret": "s3cret", "browser_url": "http://127.0.0.1:3001"}
			],
			"admins": ["gitea:root"]
		}}
	}`, true)
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
	b := parse(t, `{
		"secret_managers": [{"vault": {"endpoint": "http://vault:8200", "mount_point": "secret", "kv_version": 2,
			"token_auth": {"token": "root"}}}],
		"ui": {"auth": {"providers": [{"platform": "gitea", "client_id": "id",
			"client_secret": {"ref": {"path": "ui/gitea", "key": "client_secret"}}}], "secret_manager": "vault"}}
	}`, true)
	if got := b.GetSecretManagers()[0].GetVault().GetTokenAuth().GetToken(); got != "root" {
		t.Errorf("vault token = %q", got)
	}
	if ref := b.GetUi().GetAuth().GetProviders()[0].GetClientSecret().GetRef(); ref.GetPath() != "ui/gitea" || ref.GetKey() != "client_secret" {
		t.Errorf("client_secret ref = %v", ref)
	}
}

func TestARedisPasswordTakesTheFileKeyForm(t *testing.T) {
	b := parse(t, `{"datasources":[{"redis":{"password":{"file":"/run/secrets/redis.env","key":"REDIS_PASSWORD"}}}]}`, true)
	pw := b.GetDatasources()[0].GetRedis().GetPassword()
	if pw.GetFile() != "/run/secrets/redis.env" || pw.GetKey() != "REDIS_PASSWORD" {
		t.Errorf("password = %v", pw)
	}
}

func TestOldOptionNamesAreNotDroppedSilentlyWhenUnknownFieldsAreIgnored(t *testing.T) {
	// A lenient parse discards unknown fields: without the rename, a bundle
	// written with the old names would load with every strategy off.
	b := parse(t, `{
		"rules": [{"platform": "gitea", "datasource": "mongo", "self_heal": true, "env_as_branch": true, "env_as_pr": true, "env_as_tags": true}],
		"datasources": [{"s3": {"env_as_prefix": true}}]
	}`, false)
	r := b.GetRules()[0]
	if !r.GetBranchAsEnv() || !r.GetPrAsEnv() || !r.GetTagAsEnv() {
		t.Errorf("old names dropped: %v", r)
	}
	if !b.GetDatasources()[0].GetS3().GetPrefixAsEnv() {
		t.Error("s3.env_as_prefix dropped")
	}
}
