package config_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gateway-service/internal/config"
)

const goodSecret = "0123456789abcdef0123456789abcdef"

// internalAuthEnv sets a minimal valid environment for LoadEnv in the given
// APP_ENV, leaving the internal-auth variables for the test to set.
func internalAuthEnv(t *testing.T, appEnv string) {
	t.Helper()
	cfg := filepath.Join(t.TempDir(), "config.cue")
	if err := os.WriteFile(cfg, []byte("bundle: {}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for k, v := range map[string]string{
		"APP_ENV": appEnv, "LOG_LEVEL": "INFO", "CONFIG_PATH": cfg,
		"ORCHESTRATOR_ADDR": "orchestrator:50051", "GATEWAY_ADDR": ":5657", "AGENT_ADDR": "localhost:50052",
		"INTERNAL_AUTH_SECRET": "", "INTERNAL_AUTH_SECRET_FILE": "", "INTERNAL_AUTH_INSECURE": "",
		"GRPC_TLS_CERT": "", "GRPC_TLS_KEY": "", "GRPC_TLS_CA": "", "GATEWAY_TLS_CERT": "", "GATEWAY_TLS_KEY": "",
	} {
		t.Setenv(k, v)
	}
	if appEnv == "production" { // production also needs TLS material: give it files that exist
		dir := t.TempDir()
		for _, name := range []string{"c.crt", "c.key", "s.crt", "s.key"} {
			if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		t.Setenv("GRPC_TLS_CERT", filepath.Join(dir, "c.crt"))
		t.Setenv("GRPC_TLS_KEY", filepath.Join(dir, "c.key"))
		t.Setenv("GATEWAY_TLS_CERT", filepath.Join(dir, "s.crt"))
		t.Setenv("GATEWAY_TLS_KEY", filepath.Join(dir, "s.key"))
	}
}

func TestInternalAuth_ProductionRefusesToStartWithoutASecret(t *testing.T) {
	internalAuthEnv(t, "production")
	_, err := config.LoadEnv()
	if err == nil || !strings.Contains(err.Error(), "INTERNAL_AUTH_SECRET") {
		t.Fatalf("err = %v", err)
	}
}

func TestInternalAuth_ProductionMayRunUnsignedOnlyOnPurpose(t *testing.T) {
	internalAuthEnv(t, "production")
	t.Setenv("INTERNAL_AUTH_INSECURE", "true")
	env, err := config.LoadEnv()
	if err != nil {
		t.Fatal(err)
	}
	if env.InternalAuthEnabled() {
		t.Fatal("no secret was given: signing must be off")
	}
}

func TestInternalAuth_AShortSecretIsRefused(t *testing.T) {
	internalAuthEnv(t, "demo")
	t.Setenv("INTERNAL_AUTH_SECRET", "too-short")
	if _, err := config.LoadEnv(); err == nil || !strings.Contains(err.Error(), "at least") {
		t.Fatalf("err = %v", err)
	}
}

func TestInternalAuth_SecretFromEnvOrFile(t *testing.T) {
	internalAuthEnv(t, "demo")
	t.Setenv("INTERNAL_AUTH_SECRET", goodSecret)
	env, err := config.LoadEnv()
	if err != nil || !env.InternalAuthEnabled() || env.InternalAuthSecret != goodSecret {
		t.Fatalf("env: %v, %v", env, err)
	}

	internalAuthEnv(t, "demo")
	f := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(f, []byte(goodSecret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("INTERNAL_AUTH_SECRET_FILE", f)
	env, err = config.LoadEnv()
	if err != nil || env.InternalAuthSecret != goodSecret {
		t.Fatalf("file: %v, %v", env, err)
	}

	t.Setenv("INTERNAL_AUTH_SECRET_FILE", filepath.Join(t.TempDir(), "missing"))
	if _, err := config.LoadEnv(); err == nil {
		t.Fatal("a secret file that cannot be read must be an error, not a gateway that quietly runs unsigned")
	}
}

func TestInternalAuth_TheSecretIsNeverLogged(t *testing.T) {
	internalAuthEnv(t, "demo")
	t.Setenv("INTERNAL_AUTH_SECRET", goodSecret)
	env, err := config.LoadEnv()
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{env.String(), env.GoString(), env.LogValue().String()} {
		if strings.Contains(s, goodSecret) {
			t.Fatalf("the secret leaked into %q", s)
		}
	}
}

func TestInternalAuth_DevelopmentNeedsNoSecret(t *testing.T) {
	internalAuthEnv(t, "demo")
	env, err := config.LoadEnv()
	if err != nil || env.InternalAuthEnabled() {
		t.Fatalf("env: %v, err: %v", env, err)
	}
}
