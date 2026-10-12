package config

import (
	"encoding/json"
	"testing"
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
