// A closed schema for examples/configs/settings.json: a key it does not declare fails.
//
// Put it at the root of the schema repository as settings.json.cue. A typo
// (databse.host) or a key nobody reviewed is refused before it reaches the sink:
//   debgu: conflicting values "true" and "not a key this schema declares"
//
// The last line does it: every key whose name is not one of the declared ones must
// equal that sentence, which no value does. (close({ … }) does not close a schema
// here: the schema and the file's data are checked as one CUE package, and close()
// does not stop a field that another file of the package declares.)
//
// The fields are quoted dotted keys, as the file is flattened before it is checked.

"app.name":        string & !=""
"app.version":     =~"^[0-9]+\\.[0-9]+\\.[0-9]+$"
"app.environment": "production" | "staging" | "development"
"app.debug":       bool

"server.host":            string
"server.port":            int & >=1024 & <=65535
"server.workers":         int & >=1 & <=64
"server.timeout_seconds": int & >=1 & <=300

"database.host":      string & !=""
"database.port":      int & >=1 & <=65535
"database.name":      string & !=""
"database.user":      string & !=""
"database.password":  string & !="" @secret()
"database.pool_size": int & >=1 & <=100
"database.ssl":       bool

"cache.backend":     "redis" | "memcached"
"cache.host":        string
"cache.port":        int & >=1 & <=65535
"cache.ttl_seconds": int & >=0

"feature_flags.new_ui":    bool
"feature_flags.beta_api":  bool
"feature_flags.analytics": bool

"limits.rate_limit_rps":   int & >=1
"limits.max_body_size_mb": int & >=1 & <=100
"limits.max_connections":  int & >=1

// Every other key is refused. Keep this list in step with the keys above.
[!~"^(app\\.(name|version|environment|debug)|server\\.(host|port|workers|timeout_seconds)|database\\.(host|port|name|user|password|pool_size|ssl)|cache\\.(backend|host|port|ttl_seconds)|feature_flags\\.(new_ui|beta_api|analytics)|limits\\.(rate_limit_rps|max_body_size_mb|max_connections))$"]: "not a key this schema declares"
