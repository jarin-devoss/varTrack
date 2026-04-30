package config_test

import (
	"os"
	"testing"

	"gateway-service/internal/config"
)

func TestEnvOr_Default(t *testing.T) {
	key := "TEST_ENVVAR_NONEXISTENT_" + t.Name()
	os.Unsetenv(key)

	got := config.EnvOr(key, "fallback")
	if got != "fallback" {
		t.Errorf("EnvOr(%q, %q) = %q, want %q", key, "fallback", got, "fallback")
	}
}

func TestEnvOr_Set(t *testing.T) {
	key := "TEST_ENVVAR_SET_" + t.Name()
	t.Setenv(key, "real-value")

	got := config.EnvOr(key, "fallback")
	if got != "real-value" {
		t.Errorf("EnvOr(%q, %q) = %q, want %q", key, "fallback", got, "real-value")
	}
}

func TestLoadEnv_MissingRequired(t *testing.T) {
	// Clear all required env vars to force validation failure.
	for _, key := range []string{
		"APP_ENV", "LOG_LEVEL", "ORCHESTRATOR_ADDR",
		"GATEWAY_ADDR", "CONFIG_PATH",
	} {
		t.Setenv(key, "")
	}

	_, err := config.LoadEnv()
	if err == nil {
		t.Fatal("expected error when required env vars are missing")
	}
}
