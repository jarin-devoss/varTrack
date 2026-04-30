// Example: Strict schema with closed structs — no extra keys allowed.
// Useful to enforce that all config keys are explicitly declared.
//
// CUE closed structs use the `close()` builtin or the #Type literal syntax.
// A "closed" struct rejects any field not listed in the schema.

package schema

// Closed struct: push with any unknown key will fail validation.
#StrictConfig: close({
	database_url:  =~"^(postgres|mysql|mongodb)://"  // must be a valid DSN
	db_pool_size:  >=1 & <=50
	redis_url:     =~"^redis://"
	log_level:     "debug" | "info" | "warn" | "error"
	port:          >=1024 & <=65535
	debug:         bool
	secret_key:    string & len(secret_key) >= 32  // minimum 32-char secret
})

// Partial schema: only validate specific keys, allow unknown extras.
#PartialConfig: {
	port:      >=1024 & <=65535
	log_level: "debug" | "info" | "warn" | "error"
	debug:     bool
	// ...other keys accepted but not validated
}

// Versioned schema: enforce semantic version string.
#VersionedConfig: {
	app_version: =~"^v?\\d+\\.\\d+\\.\\d+(-[a-zA-Z0-9.]+)?$"
	...
}
