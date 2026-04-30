// Example: CUE validation schema for a config file in Git.
//
// Place this file in your repo alongside the config files. VarTrack fetches
// the schema and validates the parsed config before writing to any sink.
// If validation fails, the ETL job is rejected with a clear error message.
//
// Push the schema path in the settings.json rule:
//   { "schema_path": "schemas/app_schema.cue" }

package schema

#AppConfig: {
	app: {
		name:    string & !=""
		version: =~"^\\d+\\.\\d+\\.\\d+$"  // semver
		env:     "production" | "staging" | "development"
		debug:   bool
	}

	server: {
		host:    string
		port:    >=1 & <=65535
		workers: >=1 & <=64
		timeout: >=1 & <=300
	}

	database: {
		host:     string & !=""
		port:     >=1 & <=65535
		name:     string & !=""
		user:     string & !=""
		password: string & !=""  // resolved from @secret() before validation
		pool:     >=1 & <=100
		ssl:      bool
	}

	cache?: {
		host: string
		port: >=1 & <=65535
		db:   >=0 & <=15
		ttl:  >=0
	}

	feature_flags?: {[string]: bool}
}
