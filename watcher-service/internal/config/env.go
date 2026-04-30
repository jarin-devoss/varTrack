// Package config loads environment variables for the watcher-service.
// Follows the same pattern as gateway-service/internal/config/env.go:
//   - no os.Setenv — all values are read once into a local struct
//   - sensitive fields are masked in log output
//   - EnvOr(key, default) for optional values
package config

import (
	"bufio"
	"fmt"
	"log"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

// Env holds all environment variables consumed by the watcher-service.
type Env struct {
	AppEnv       string // APP_ENV
	LogLevel     string // LOG_LEVEL
	AdminAddr    string // ADMIN_ADDR — health/metrics HTTP server
	ConfigPath   string // CONFIG_PATH — path to the CUE config bundle
	OrchestratorAddr string        // ORCHESTRATOR_ADDR — orchestrator gRPC address (host:port)
	HealTimeout      time.Duration // HEAL_TIMEOUT (default 30s)
	PollInterval time.Duration // POLL_INTERVAL (default 60s)
	StateDir     string        // STATE_DIR — directory for local state snapshots

	// RedisURL is optional.  When set, watcher baselines are persisted to Redis
	// instead of the local filesystem.  Useful when running multiple watcher
	// replicas so they share the same baseline.
	// Resolved from the CUE bundle datasource tagged by global_tags.watcher_state_redis.
	RedisURL string

	// TLS for outbound HTTP calls to the orchestrator.
	// Empty strings → plain HTTP (safe for local dev and tests).
	// TLSCAFile     — CA cert to verify the orchestrator server certificate.
	// TLSCertFile / TLSKeyFile — client cert for mTLS (both required together).
	TLSCAFile   string // TLS_CA_FILE
	TLSCertFile string // TLS_CERT_FILE
	TLSKeyFile  string // TLS_KEY_FILE
}

// LoadEnv reads and validates environment variables.
// Returns an error if any required variable is missing or invalid.
func LoadEnv() (*Env, error) {
	loadDotEnv()

	env := &Env{
		AppEnv:       EnvOr("APP_ENV", "development"),
		LogLevel:     strings.ToUpper(EnvOr("LOG_LEVEL", "INFO")),
		AdminAddr:    EnvOr("ADMIN_ADDR", ":9091"),
		ConfigPath:   EnvOr("CONFIG_PATH", "config.cue"),
		OrchestratorAddr: EnvOr("ORCHESTRATOR_ADDR", "localhost:50051"),
		StateDir:     EnvOr("STATE_DIR", "/tmp/watcher-state"),
		TLSCAFile:    EnvOr("TLS_CA_FILE", ""),
		TLSCertFile:  EnvOr("TLS_CERT_FILE", ""),
		TLSKeyFile:   EnvOr("TLS_KEY_FILE", ""),
	}

	var err error
	if env.HealTimeout, err = parseDuration("HEAL_TIMEOUT", "30s"); err != nil {
		return nil, err
	}
	if env.PollInterval, err = parseDuration("POLL_INTERVAL", "60s"); err != nil {
		return nil, err
	}
	if env.ConfigPath == "" {
		return nil, fmt.Errorf("CONFIG_PATH must not be empty")
	}

	// Resolve Redis URL from the CUE bundle datasource (no REDIS_URL env var).
	env.RedisURL = RedisURLFromBundle(env.ConfigPath)

	// Production requires mTLS to the orchestrator.
	if env.IsProduction() {
		if env.TLSCAFile == "" {
			return nil, fmt.Errorf("production mode requires TLS_CA_FILE to verify the orchestrator")
		}
		if env.TLSCertFile == "" || env.TLSKeyFile == "" {
			return nil, fmt.Errorf("production mode requires TLS_CERT_FILE and TLS_KEY_FILE for mTLS to the orchestrator")
		}
	}

	return env, nil
}

// dotEnvMap stores values loaded from the shared .env file.
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

// EnvOr returns the environment variable or, if not set in the real
// environment, a value from the loaded .env file, or the fallback default.
func EnvOr(key, defaultVal string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	if v, ok := dotEnvMap[key]; ok {
		return v
	}
	return defaultVal
}

// EnvInt returns the integer value of the named environment variable,
// or defaultVal if the variable is not set or cannot be parsed.
func EnvInt(key string, defaultVal int) int {
	v := os.Getenv(key)
	if v == "" {
		return defaultVal
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		slog.Warn("watcher: invalid env var, using default",
			"key", key, "value", v, "default", defaultVal)
		return defaultVal
	}
	return n
}

// IsProduction reports whether the APP_ENV is set to "production".
func (e *Env) IsProduction() bool { return e.AppEnv == "production" }

// LogValue implements slog.LogValuer — masks no secrets (watcher has none).
func (e *Env) LogValue() slog.Value {
	return slog.GroupValue(
		slog.String("app_env", e.AppEnv),
		slog.String("log_level", e.LogLevel),
		slog.String("admin_addr", e.AdminAddr),
		slog.String("config_path", e.ConfigPath),
		slog.String("orchestrator_addr", e.OrchestratorAddr),
		slog.Duration("heal_timeout", e.HealTimeout),
		slog.Duration("poll_interval", e.PollInterval),
		slog.String("state_dir", e.StateDir),
		slog.Bool("redis_state", e.RedisURL != ""),
		slog.Bool("tls_ca_set", e.TLSCAFile != ""),
		slog.Bool("tls_cert_set", e.TLSCertFile != ""),
	)
}

func parseDuration(key, defaultVal string) (time.Duration, error) {
	raw := EnvOr(key, defaultVal)
	d, err := time.ParseDuration(raw)
	if err != nil {
		return 0, fmt.Errorf("invalid duration for %s=%q: %w", key, raw, err)
	}
	return d, nil
}
