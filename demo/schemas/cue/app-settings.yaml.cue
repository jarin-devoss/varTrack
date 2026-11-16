{
	"app.name":   string
	"app.region": string
	"app.env":    string

	// The demo's file quotes its numbers and booleans ("10", "true"): the schema checks them as numbers
	// and booleans all the same, and the file decides how each sink stores them.
	"database.pool_size":          int & >=1
	"database.max_connections":    int & >=1 & <=1000
	"database.connect_timeout_ms": int & >=0
	"database.password":           string @secret()

	"cache.ttl_seconds": int & >=0
	"cache.backend":     string

	"api.rate_limit":  int & >=0
	"api.timeout_ms":  int & >=0
	"api.version":     string
	"api.secret_key":  string @secret()

	"features.dark_mode":   bool
	"features.analytics":   bool
	"features.debug_mode":  bool

	"observability.log_level":       string & =~"^(DEBUG|INFO|WARN|ERROR)$"
	"observability.service_mesh":    string
	"observability.tracing_enabled": bool

	...
}
