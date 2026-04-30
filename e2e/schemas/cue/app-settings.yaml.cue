{
	"app.name":   string
	"app.region": string
	"app.env":    string

	"database.pool_size":          string
	"database.max_connections":    string
	"database.connect_timeout_ms": string
	"database.password":           string @secret()

	"cache.ttl_seconds": string
	"cache.backend":     string

	"api.rate_limit":  string
	"api.timeout_ms":  string
	"api.version":     string
	"api.secret_key":  string @secret()

	"features.dark_mode":   string
	"features.analytics":   string
	"features.debug_mode":  string

	"observability.log_level":       string & =~"^(DEBUG|INFO|WARN|ERROR)$"
	"observability.service_mesh":    string
	"observability.tracing_enabled": string

	...
}
