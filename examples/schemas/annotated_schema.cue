// Full CUE schema example with @secret() and @logger() annotations.
//
// examples/configs/annotated.yaml is a file that passes it, and
// examples/bundles/09_vault_secrets.cue the bundle that uses it.
//
// Its name is not a file's name, so nothing finds it by name: a rule's schema_map
// names it (schema: "annotated_schema.cue", its path in the schema repository). To
// have it found by name instead, store it as annotated.yaml.cue at the root.
//
// The fields are at the top level, not inside a #Definition: `cue vet` checks the data only against top-level
// fields. A key with a dot is written as a quoted label ("database.host"), as the file is flattened first.
//
// TYPES: a value is checked as the file wrote it. `port: 8080` is an int in YAML, JSON, TOML and HCL; `"8080"` in
// a YAML file, and every value of a .env, .properties, INI or XML file, is text that reads as an int. `int`,
// `float`, `bool` and ranges check either; `string & =~"^[0-9]+$"` still works. A value that does not read as the
// type fails, naming the key and the value.
//
// ANNOTATIONS:
//   @secret()          — value is a Vault path; resolved before writing to any sink.
//                        In dry-run reports the real value is replaced with ***.
//   @secret(ref="x")   — resolves via the secret manager tagged "x" in the bundle
//                        (09_vault_secrets.cue declares "default" and "auth-vault").
//   Only a field the schema marks is read from Vault: a "@secret(…)" value in a field
//   it does not mark is written as the text it is.
//   @logger()          — emits a structured log line whenever the field value changes.
//   @secret() @logger() — both: logs the change but never reveals the secret value.
//
// VAULT PATH FORMAT in your config files (YAML, JSON, TOML, .env…):
//   @secret(mount/path#field)
//   e.g.  @secret(secret/myapp/db#password)
//            └─ mount: "secret"
//                 └─ path:  "myapp/db"
//                      └─ field: "password"

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
"api.stripe_key":   string & !="" @secret()
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
