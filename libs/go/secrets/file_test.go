package secrets_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"vartrack/libs/secrets"
)

func writeSecretFile(t *testing.T, name, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestReadSecretFile(t *testing.T) {
	env := writeSecretFile(t, "db.env", "# database\nexport USER=app\nPASSWORD=\"p@ss word\"\nTOKEN='abc'\nEMPTY=\n")
	props := writeSecretFile(t, "app.properties", "db.password = s3cret\n")
	jsonFile := writeSecretFile(t, "creds.json", `{"db": {"password": "j-pw", "port": 5432}, "api.key": "flat"}`)
	yamlFile := writeSecretFile(t, "creds.yaml", "db:\n  password: y-pw\n  replicas: [a, b]\n")
	whole := writeSecretFile(t, "token", "  whole-file\n")

	tests := []struct {
		name, path, key, want string
	}{
		{"whole file, trimmed", whole, "", "whole-file"},
		{".env value", env, "USER", "app"},
		{".env double-quoted", env, "PASSWORD", "p@ss word"},
		{".env single-quoted", env, "TOKEN", "abc"},
		{".env empty value", env, "EMPTY", ""},
		{".properties", props, "db.password", "s3cret"},
		{"JSON dotted path", jsonFile, "db.password", "j-pw"},
		{"JSON number", jsonFile, "db.port", "5432"},
		{"JSON key with a dot", jsonFile, "api.key", "flat"},
		{"YAML dotted path", yamlFile, "db.password", "y-pw"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := secrets.ReadSecretFile(tc.path, tc.key)
			if err != nil || got != tc.want {
				t.Errorf("ReadSecretFile(%s, %q) = %q, %v; want %q", filepath.Base(tc.path), tc.key, got, err, tc.want)
			}
		})
	}

	errs := []struct {
		name, path, key, want string
	}{
		{"missing key", env, "NOPE", `has no key "NOPE"`},
		{"key holding an object", yamlFile, "db", "not a single value"},
		{"key holding a list", yamlFile, "db.replicas", "not a single value"},
		{"missing file", filepath.Join(t.TempDir(), "nope.env"), "X", "secret file"},
	}
	for _, tc := range errs {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := secrets.ReadSecretFile(tc.path, tc.key); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("err = %v, want one containing %q", err, tc.want)
			}
		})
	}
}
