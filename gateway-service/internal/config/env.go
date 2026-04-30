// Package config loads environment variables and CUE bundle configuration.
package config

import (
	"bufio"
	"fmt"
	"log"
	"log/slog"
	"net"
	"os"
	"strings"
)

// Env holds the shared environment variables consumed by all VarTrack services.
type Env struct {
	AppEnv           string // APP_ENV
	LogLevel         string // LOG_LEVEL
	OrchestratorAddr string // ORCHESTRATOR_ADDR — dial address for the orchestrator gRPC service
	GatewayAddr      string // GATEWAY_ADDR — listen address for this gateway (e.g. ":5657")
	AgentAddr        string // AGENT_ADDR — dial address for the agent gRPC service
	VaultSecret      string // VAULT_SECRET — sensitive: masked in logs
	ConfigPath       string // CONFIG_PATH — path to the CUE bundle file
	GRPCTlsCa        string // GRPC_TLS_CA — path to CA cert for outbound gRPC
	GRPCTlsCert      string // GRPC_TLS_CERT — path to client cert (mTLS)
	GRPCTlsKey       string // GRPC_TLS_KEY — path to client key (mTLS)
	GatewayTlsCert   string // GATEWAY_TLS_CERT — inbound TLS cert for the HTTP server
	GatewayTlsKey    string // GATEWAY_TLS_KEY — inbound TLS key for the HTTP server

	// RedisURL is optional.  When set, delivery nonces are persisted to Redis
	// so that multi-replica gateway deployments share the same nonce window
	// and correctly block cross-replica replay attacks.
	// Resolved from the CUE bundle datasource tagged by global_tags.gateway_nonce_redis.
	RedisURL string
}

func (e *Env) GetOrchestratorAddr() string { return e.OrchestratorAddr }
func (e *Env) GetGatewayAddr() string      { return e.GatewayAddr }
func (e *Env) GetAgentAddr() string        { return e.AgentAddr }

// GetTLSCert and GetTLSKey expose the inbound HTTP TLS pair through the
// same config object as all other settings.
func (e *Env) GetTLSCert() string { return e.GatewayTlsCert }
func (e *Env) GetTLSKey() string  { return e.GatewayTlsKey }

func (e *Env) IsProduction() bool {
	return e.AppEnv == "production"
}

// LogValue implements slog.LogValuer to mask sensitive fields automatically.
func (e *Env) LogValue() slog.Value {
	vaultSecretDisplay := "[NOT SET]"
	if e.VaultSecret != "" {
		vaultSecretDisplay = "[REDACTED]"
	}

	return slog.GroupValue(
		slog.String("app_env", e.AppEnv),
		slog.String("log_level", e.LogLevel),
		slog.String("orchestrator_addr", e.OrchestratorAddr),
		slog.String("gateway_addr", e.GatewayAddr),
		slog.String("agent_addr", e.AgentAddr),
		slog.String("config_path", e.ConfigPath),
		slog.String("vault_secret", vaultSecretDisplay),
		slog.String("grpc_tls_ca", boolToSet(e.GRPCTlsCa != "")),
		slog.String("grpc_tls_cert", boolToSet(e.GRPCTlsCert != "")),
		slog.String("grpc_tls_key", boolToSet(e.GRPCTlsKey != "")),
		slog.String("gateway_tls_cert", boolToSet(e.GatewayTlsCert != "")),
		slog.String("gateway_tls_key", boolToSet(e.GatewayTlsKey != "")),
	)
}

// GoString implements fmt.GoStringer to prevent secret leaks via %#v format.
func (e *Env) GoString() string {
	return e.String()
}

// String implements fmt.Stringer to prevent secret leaks via fmt.Sprintf.
func (e *Env) String() string {
	vaultDisplay := "[NOT SET]"
	if e.VaultSecret != "" {
		vaultDisplay = "[REDACTED]"
	}
	return fmt.Sprintf(
		"Env{app_env=%s log_level=%s orchestrator=%s gateway=%s agent=%s vault_secret=%s config=%s}",
		e.AppEnv, e.LogLevel, e.OrchestratorAddr, e.GatewayAddr, e.AgentAddr,
		vaultDisplay, e.ConfigPath,
	)
}

func boolToSet(b bool) string {
	if b {
		return "set"
	}
	return "not set"
}

// LoadEnv loads an optional .env file, then reads environment variables.
//
// Resolution order (last wins):
//  1. .env file (if present — not required)
//  2. Real environment variables (always override .env file)
func LoadEnv() (*Env, error) {
	loadDotEnv()

	env := &Env{
		AppEnv:           EnvOr("APP_ENV", "test"),
		LogLevel:         strings.ToUpper(EnvOr("LOG_LEVEL", "INFO")),
		OrchestratorAddr: EnvOr("ORCHESTRATOR_ADDR", "localhost:50051"),
		GatewayAddr:      EnvOr("GATEWAY_ADDR", ":5657"),
		AgentAddr:        EnvOr("AGENT_ADDR", "localhost:50052"),
		VaultSecret:      EnvOr("VAULT_SECRET", ""),
		ConfigPath:       EnvOr("CONFIG_PATH", "config.cue"),
		GRPCTlsCa:        EnvOr("GRPC_TLS_CA", ""),
		GRPCTlsCert:      EnvOr("GRPC_TLS_CERT", ""),
		GRPCTlsKey:       EnvOr("GRPC_TLS_KEY", ""),
		GatewayTlsCert:   EnvOr("GATEWAY_TLS_CERT", ""),
		GatewayTlsKey:    EnvOr("GATEWAY_TLS_KEY", ""),
	}

	env.AppEnv = strings.ToLower(strings.TrimSpace(env.AppEnv))

	if err := env.validate(); err != nil {
		return nil, err
	}

	// Resolve Redis URL from the CUE bundle datasource (no REDIS_URL env var).
	env.RedisURL = RedisURLFromBundle(env.ConfigPath)

	return env, nil
}

// dotEnvMap stores values loaded from .env files.
// Using a package-level map instead of os.Setenv avoids mutating the
// shared process environment from a potentially concurrent context.
var dotEnvMap = map[string]string{}

func loadDotEnv() {
	candidates := []string{
		os.Getenv("ENV_FILE"),
		".env",
	}
	for _, path := range candidates {
		if path == "" {
			continue
		}
		if _, err := os.Stat(path); err == nil {
			if err := parseDotEnv(path); err != nil {
				log.Printf("Warning: failed to parse %s: %v", path, err)
			} else {
				log.Printf("Loaded env from %s", path)
			}
			return
		}
	}
}

func parseDotEnv(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if len(value) >= 2 && ((value[0] == '"' && value[len(value)-1] == '"') || (value[0] == '\'' && value[len(value)-1] == '\'')) {
			value = value[1 : len(value)-1]
		} else if idx := strings.Index(value, " #"); idx != -1 {
			value = strings.TrimSpace(value[:idx])
		}
		if _, exists := os.LookupEnv(key); !exists {
			dotEnvMap[key] = value
		}
	}
	return scanner.Err()
}

// validate checks all environment variables at startup, preventing
// "half-started" states. Service addresses are validated using
// net.SplitHostPort to catch malformed values early.
func (e *Env) validate() error {
	switch e.AppEnv {
	case "production", "test", "demo", "staging", "development":
	default:
		return fmt.Errorf("APP_ENV must be 'production', 'test', 'demo', 'staging', or 'development', got %q", e.AppEnv)
	}

	switch e.LogLevel {
	case "DEBUG", "INFO", "WARN", "ERROR":
	default:
		return fmt.Errorf("LOG_LEVEL must be DEBUG|INFO|WARN|ERROR, got %q", e.LogLevel)
	}

	// Validate service addresses using net.SplitHostPort.
	addrVars := map[string]string{
		"ORCHESTRATOR_ADDR": e.OrchestratorAddr,
		"GATEWAY_ADDR":      e.GatewayAddr,
		"AGENT_ADDR":        e.AgentAddr,
	}
	for name, addr := range addrVars {
		if addr == "" {
			return fmt.Errorf("%s must be set", name)
		}

		host, port, err := net.SplitHostPort(addr)
		if err != nil {
			return fmt.Errorf("%s=%q is not a valid host:port address: %w", name, addr, err)
		}

		if port == "" {
			return fmt.Errorf("%s=%q has an empty port segment", name, addr)
		}

		if host != "" && net.ParseIP(host) == nil && host != "localhost" {
			// Basic host checks could occur here, but net.SplitHostPort covers brackets.
		}
	}

	// Config path — verify file exists at startup.
	if _, err := os.Stat(e.ConfigPath); err != nil {
		return fmt.Errorf("CONFIG_PATH=%q: file not accessible: %w", e.ConfigPath, err)
	}

	// TLS cert pairs — both or neither must be set.
	if err := validateTLSPair("GRPC_TLS_CERT", e.GRPCTlsCert, "GRPC_TLS_KEY", e.GRPCTlsKey); err != nil {
		return err
	}

	gwCert := e.GatewayTlsCert
	gwKey := e.GatewayTlsKey
	if err := validateTLSPair("GATEWAY_TLS_CERT", gwCert, "GATEWAY_TLS_KEY", gwKey); err != nil {
		return err
	}

	// Production requires mTLS — fail fast rather than silently running insecure.
	if e.IsProduction() {
		if e.GRPCTlsCert == "" || e.GRPCTlsKey == "" {
			return fmt.Errorf("production mode requires GRPC_TLS_CERT and GRPC_TLS_KEY for mTLS to the orchestrator")
		}
		if e.GatewayTlsCert == "" || e.GatewayTlsKey == "" {
			return fmt.Errorf("production mode requires GATEWAY_TLS_CERT and GATEWAY_TLS_KEY for inbound TLS")
		}
	}

	if e.GRPCTlsCa != "" {
		if _, err := os.Stat(e.GRPCTlsCa); err != nil {
			return fmt.Errorf("GRPC_TLS_CA=%q: file not accessible: %w", e.GRPCTlsCa, err)
		}
	}

	return nil
}

// validateTLSPair ensures that if either cert or key is set, both are set,
// and both files exist on disk.
func validateTLSPair(certName, certPath, keyName, keyPath string) error {
	hasCert := certPath != ""
	hasKey := keyPath != ""

	if hasCert != hasKey {
		return fmt.Errorf("%s and %s must both be set or both be empty", certName, keyName)
	}

	if hasCert {
		if _, err := os.Stat(certPath); err != nil {
			return fmt.Errorf("%s=%q: file not accessible: %w", certName, certPath, err)
		}
	}
	if hasKey {
		if _, err := os.Stat(keyPath); err != nil {
			return fmt.Errorf("%s=%q: file not accessible: %w", keyName, keyPath, err)
		}
	}
	return nil
}

// EnvOr returns the environment variable or, if not set in the real
// environment, a value from the loaded .env file, or the fallback default.
func EnvOr(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	if v, ok := dotEnvMap[key]; ok {
		return v
	}
	return fallback
}
