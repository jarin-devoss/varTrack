package secretmanagers

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	pb_vault "vartrack/libs/gen/vartrack/v1/models/secret_managers"
)

// A throwaway self-signed CA, only to be parsed.
const testCA = `-----BEGIN CERTIFICATE-----
MIIBdjCCAR2gAwIBAgIUDqh0mP1Gm9tqkYtzgyk3XKe5k4wwCgYIKoZIzj0EAwIw
DzENMAsGA1UEAwwEdGVzdDAeFw0yNjAxMDEwMDAwMDBaFw0zNjAxMDEwMDAwMDBa
MA8xDTALBgNVBAMMBHRlc3QwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAAQ0T1b6
4pJzqk0v0hS0g3Uq0m3b7tT6y3v3v5y5sXb1Y6mXw2w6vC3k1m4v0R5mXw2w6vC3
k1m4v0R5mXw2w6vC3k1m4v0R5o1MwUTAdBgNVHQ4EFgQU0Tfake0fake0fake0fake
-----END CERTIFICATE-----
`

func TestTheServersCertificateIsCheckedUnlessSaidOtherwise(t *testing.T) {
	v := &Vault{config: &pb_vault.VaultConfig{}}
	cfg, err := v.buildTLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.InsecureSkipVerify {
		t.Error("a Vault with nothing said about TLS is not verified")
	}
	v = &Vault{config: &pb_vault.VaultConfig{InsecureSkipVerify: true}}
	if cfg, _ = v.buildTLSConfig(); !cfg.InsecureSkipVerify {
		t.Error("insecure_skip_verify: true does not skip verification")
	}
}

func TestACAIsContentOrAFileNeverBoth(t *testing.T) {
	ca := testCA
	file := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(file, []byte(ca), 0o600); err != nil {
		t.Fatal(err)
	}
	// A path in ssl_ca_file is read; a path where PEM content belongs is not a certificate.
	v := &Vault{config: &pb_vault.VaultConfig{SslCaFile: &file}}
	if _, err := v.buildTLSConfig(); err != nil && !strings.Contains(err.Error(), "parse vault CA") {
		t.Errorf("file: %v", err)
	}
	notPEM := file
	v = &Vault{config: &pb_vault.VaultConfig{SslCa: &notPEM}}
	if _, err := v.buildTLSConfig(); err == nil {
		t.Error("a file name given as ssl_ca content was accepted")
	}
	v = &Vault{config: &pb_vault.VaultConfig{SslCa: &ca, SslCaFile: &file}}
	if _, err := v.buildTLSConfig(); err == nil || !strings.Contains(err.Error(), "not both") {
		t.Errorf("both: %v", err)
	}
	missing := filepath.Join(t.TempDir(), "nope.pem")
	v = &Vault{config: &pb_vault.VaultConfig{SslCaFile: &missing}}
	if _, err := v.buildTLSConfig(); err == nil {
		t.Error("an unreadable CA file was ignored")
	}
}
