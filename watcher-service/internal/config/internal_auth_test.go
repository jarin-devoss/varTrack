package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const goodSecret = "0123456789abcdef0123456789abcdef"

// authEnv sets the variables LoadEnv reads, with the internal-auth ones cleared.
func authEnv(t *testing.T, appEnv string) {
	t.Helper()
	for k, v := range map[string]string{
		"APP_ENV": appEnv, "LOG_LEVEL": "INFO", "CONFIG_PATH": "config.cue",
		"INTERNAL_AUTH_SECRET": "", "INTERNAL_AUTH_SECRET_FILE": "",
		"INTERNAL_AUTH_WATCH_SECRET": "", "INTERNAL_AUTH_WATCH_SECRET_FILE": "",
		"INTERNAL_AUTH_INSECURE": "", "TLS_CA_FILE": "", "TLS_CERT_FILE": "", "TLS_KEY_FILE": "",
	} {
		t.Setenv(k, v)
	}
	if appEnv == "production" {
		t.Setenv("TLS_CA_FILE", "ca.pem")
		t.Setenv("TLS_CERT_FILE", "c.pem")
		t.Setenv("TLS_KEY_FILE", "k.pem")
	}
}

func TestInternalAuth_ProductionRefusesToStartWithoutASecret(t *testing.T) {
	authEnv(t, "production")
	if _, err := LoadEnv(); err == nil || !strings.Contains(err.Error(), "INTERNAL_AUTH_SECRET") {
		t.Fatalf("err = %v", err)
	}
}

func TestInternalAuth_ProductionMayRunUnsignedOnlyOnPurpose(t *testing.T) {
	authEnv(t, "production")
	t.Setenv("INTERNAL_AUTH_INSECURE", "yes")
	env, err := LoadEnv()
	if err != nil || env.InternalAuthEnabled() {
		t.Fatalf("env=%v err=%v", env, err)
	}
}

func TestInternalAuth_TheWatcherKeyWinsOverTheSharedOne(t *testing.T) {
	authEnv(t, "demo")
	t.Setenv("INTERNAL_AUTH_SECRET", goodSecret)
	t.Setenv("INTERNAL_AUTH_WATCH_SECRET", "fedcba9876543210fedcba9876543210")
	env, err := LoadEnv()
	if err != nil || env.InternalAuthSecret != "fedcba9876543210fedcba9876543210" {
		t.Fatalf("env=%v err=%v", env, err)
	}
}

func TestInternalAuth_FallsBackToTheSharedSecretAndReadsFiles(t *testing.T) {
	authEnv(t, "demo")
	t.Setenv("INTERNAL_AUTH_SECRET", goodSecret)
	if env, err := LoadEnv(); err != nil || env.InternalAuthSecret != goodSecret {
		t.Fatalf("env=%v err=%v", env, err)
	}

	authEnv(t, "demo")
	f := filepath.Join(t.TempDir(), "s")
	if err := os.WriteFile(f, []byte(goodSecret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("INTERNAL_AUTH_SECRET_FILE", f)
	if env, err := LoadEnv(); err != nil || env.InternalAuthSecret != goodSecret {
		t.Fatalf("env=%v err=%v", env, err)
	}

	t.Setenv("INTERNAL_AUTH_SECRET_FILE", filepath.Join(t.TempDir(), "missing"))
	if _, err := LoadEnv(); err == nil {
		t.Fatal("a secret file that cannot be read must be an error")
	}
}

func TestInternalAuth_AShortSecretIsRefused(t *testing.T) {
	authEnv(t, "demo")
	t.Setenv("INTERNAL_AUTH_SECRET", "short")
	if _, err := LoadEnv(); err == nil || !strings.Contains(err.Error(), "at least") {
		t.Fatalf("err = %v", err)
	}
}

func TestInternalAuth_TheSecretIsNeverLogged(t *testing.T) {
	authEnv(t, "demo")
	t.Setenv("INTERNAL_AUTH_SECRET", goodSecret)
	env, err := LoadEnv()
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(env.LogValue().String(), goodSecret) {
		t.Fatal("the secret leaked into the log value")
	}
}
