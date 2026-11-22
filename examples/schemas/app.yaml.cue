// A schema for examples/configs/app.yaml.
//
// Put it at the root of the schema repository as app.yaml.cue: a file is checked by
// the schema named after it (configs/app.yaml and services/api/app.yaml both get
// app.yaml.cue), unless a rule's schema_map names another.
//
// The schema sees the file the way the sink gets it: flattened to dotted keys,
// after root_key and the environment are taken out. So the fields are quoted
// dotted keys at the top level. A schema written inside a definition
// (#AppConfig: {…}) or as nested structs checks nothing.
//
// Keys the schema does not name are allowed: see settings.json.cue for a closed one.

"app.name":      string & !=""
"app.version":   =~"^[0-9]+\\.[0-9]+\\.[0-9]+$" // semver
"app.log_level": "debug" | "info" | "warn" | "error"
"app.debug":     bool

"server.host":            string
"server.port":            int & >=1 & <=65535
"server.timeout":         =~"^[0-9]+(ms|s|m)$"
"server.max_connections": int & >=1 & <=10000

"database.host":      string & !=""
"database.port":      int & >=1 & <=65535
"database.name":      string & !=""
"database.pool_size": int & >=1 & <=100
// In Git the value is "@secret(secret/myapp/db#password)". @secret() makes the sync
// read Vault's value in its place, so the sink never gets the placeholder.
"database.password": string & !="" @secret()

// Optional: checked only when the file has them.
"cache.host"?: string
"cache.port"?: int & >=1 & <=65535
"cache.ttl"?:  int & >=0

"auth.jwt_algorithm": "HS256" | "HS384" | "HS512"
"auth.jwt_expiry":    int & >0
"auth.jwt_secret":    string & !="" @secret() @logger() // a change is logged, its value never

// Every feature flag, whatever its name, is a boolean.
[=~"^feature_flags\\."]: bool
