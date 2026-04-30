// Full CUE schema example with @secret() and @logger() annotations.
//
// This schema validates every config push for the "myapp" tenant.
// Place this file in your schema Git repo (e.g. my-org/schemas, path: myapp.cue).
// varTrack fetches it automatically when schema_registry is configured.
//
// ANNOTATIONS:
//   @secret()          — value is a Vault path; resolved before writing to any sink.
//                        In dry-run reports the real value is replaced with ***.
//   @secret(ref="x")   — resolves via the secret manager tagged "x" in the bundle.
//   @logger()          — emits a structured log line whenever the field value changes.
//   @secret() @logger() — both: logs the change but never reveals the secret value.
//
// VAULT PATH FORMAT in your config files (YAML, JSON, TOML, .env…):
//   @secret(mount/path#field)
//   e.g.  @secret(secret/myapp/db#password)
//            └─ mount: "secret"
//                 └─ path:  "myapp/db"
//                      └─ field: "password"

#Config: {
	// ── Application ────────────────────────────────────────────────────────────

	// Port must be in the unprivileged range. Logged on every change.
	app_port: int & >=1024 & <=65535 @logger()

	// Log level is an enum — any other value fails validation before writing.
	log_level: "debug" | "info" | "warn" | "error" @logger()

	// Feature flags (boolean fields, no annotation needed).
	"feature.dark_mode":      bool
	"feature.new_onboarding": bool

	// ── Database ───────────────────────────────────────────────────────────────

	// Host and port are logged so infra changes are traceable in the audit trail.
	"database.host": string & !="" @logger()
	"database.port": int & >0      @logger()

	// Password is fetched from Vault at ETL time. The Git file contains only the
	// Vault path string: "@secret(secret/myapp/db#password)"
	// Both @secret and @logger: change is logged but value is never revealed.
	"database.password": string & !="" @secret() @logger()

	// Connection pool limits — logged so tuning changes are visible.
	"database.max_connections": int & >=1 & <=1000  @logger()
	"database.min_connections": int & >=0 & <=100   @logger()

	// ── External APIs ──────────────────────────────────────────────────────────

	// API keys — fetched from Vault, never logged as plain values.
	"api.stripe_key":  string & !="" @secret()
	"api.sendgrid_key": string & !="" @secret()

	// Third-party API endpoint — logged so endpoint changes leave a trail.
	"api.payments_url": string @logger()

	// ── Auth ───────────────────────────────────────────────────────────────────

	// JWT secrets fetched from a named Vault instance (tagged "auth-vault").
	// Use ref= when you have multiple Vault clusters.
	"auth.jwt_secret":     string & !="" @secret(ref="auth-vault")
	"auth.refresh_secret": string & !="" @secret(ref="auth-vault")

	// JWT expiry values — logged for audit.
	"auth.access_token_ttl_seconds":  int & >0 @logger()
	"auth.refresh_token_ttl_seconds": int & >0 @logger()

	// ── Redis cache ────────────────────────────────────────────────────────────

	"cache.ttl_seconds": int & >=0
	"cache.max_keys":    int & >=0

	// ── Observability ──────────────────────────────────────────────────────────

	"otel.endpoint":     string
	"otel.service_name": string
}
