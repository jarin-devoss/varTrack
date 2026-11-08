package bundle

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func loadVault(t *testing.T, auth string) (string, error) {
	t.Helper()
	file := filepath.Join(t.TempDir(), "config.cue")
	cue := "bundle: {secret_managers: [{vault: {endpoint: \"http://vault:8200\", " + auth + "}}]}\n"
	if err := os.WriteFile(file, []byte(cue), 0o600); err != nil {
		t.Fatal(err)
	}
	b, err := Load(file, Options{Strict: true, Fragment: true})
	if err != nil {
		return "", err
	}
	v := b.GetSecretManagers()[0].GetVault()
	switch {
	case v.GetTokenAuth() != nil:
		return v.GetTokenAuth().GetToken().GetValue() + v.GetTokenAuth().GetToken().GetFile(), nil
	case v.GetAppRoleAuth() != nil:
		return v.GetAppRoleAuth().GetSecretId().GetValue() + v.GetAppRoleAuth().GetSecretId().GetFile(), nil
	default:
		return v.GetUserpassAuth().GetPassword().GetValue() + v.GetUserpassAuth().GetPassword().GetFile(), nil
	}
}

func TestVaultLoginCredentialsTakeAStringOrAFile(t *testing.T) {
	for auth, want := range map[string]string{
		`token_auth: {token: "root"}`:                                          "root",
		`token_auth: {token: {file: "/run/secrets/vault-token"}}`:              "/run/secrets/vault-token",
		`app_role_auth: {role_id: "r", secret_id: {file: "/run/secrets/sid"}}`: "/run/secrets/sid",
		`userpass_auth: {username: "u", password: "pw"}`:                       "pw",
	} {
		got, err := loadVault(t, auth)
		if err != nil || got != want {
			t.Fatalf("%s: got %q, %v; want %q", auth, got, err, want)
		}
	}
}

func TestAVaultLoginCannotBeReadFromVault(t *testing.T) {
	for _, auth := range []string{
		`token_auth: {token: {ref: {path: "x", key: "y"}}}`,
		`app_role_auth: {role_id: "r", secret_id: {ref: {path: "x", key: "y"}}}`,
		`userpass_auth: {username: "u", password: {ref: {path: "x", key: "y"}}}`,
	} {
		_, err := loadVault(t, auth)
		if err == nil || !strings.Contains(err.Error(), "cannot be a {ref:}") {
			t.Fatalf("%s: err = %v", auth, err)
		}
	}
}
