// Example 17: which CUE schema checks which file, and what a failure does.
//
// A file's schema is the first of:
//   1. the first schema_map entry whose `match` matches the file's path
//   2. the file's own top-level $schema key, when the rule sets schema_from_file
//   3. <file name>.cue at the schema repository's root (configs/app.yaml → app.yaml.cue)
//   4. default.cue
//   5. none: the file is not checked
// A schema that 1 or 2 names and the repository does not have fails the file.
//
// The schema repository for this bundle could hold:
//   app.yaml.cue          examples/schemas/app.yaml.cue
//   settings.json.cue     examples/schemas/settings.json.cue
//   vars.env.cue          examples/schemas/vars.env.cue
//   api/settings.cue      named by the schema_map entry below
//
// Check a file before you push it, the way a sync would:
//   vt validate --file configs/app.yaml --datasource mongo
bundle: {
	platforms: [
		{
			github: {
				endpoint: "https://github.com"
				secret:   "my-webhook-secret"
				token:    "ghp_xxxxxxxxxxxxxxxxxxxx"
			}
		},
	]

	datasources: [
		{
			mongo: {
				endpoint:        "mongodb://mongo:27017"
				database:        "vartrack"
				collection:      "variables"
				update_strategy: "STRATEGY_DOCUMENT"
			}
		},
		{
			redis: {
				tag:   "broker"
				hosts: ["redis:6379"]
				database: 0
			}
		},
	]

	schema_registry: {
		platform: "github"
		repo:     "acme-corp/vartrack-schemas"
		branch:   "main"
	}

	rules: [
		{
			platform:     "github"
			datasource:   "mongo"
			repositories: ["acme-corp/configs"]
			file_path_map: {
				production: "services/api/production/settings.yaml"
				staging:    "services/api/staging/settings.yaml"
			}
			schema_map: [
				{match: "services/api/**/*.yaml", schema: "api/settings.cue"},
				{match: "legacy/**",              schema: "none"}, // not checked, on purpose
			]
			// A JSON, YAML or TOML file may name its schema with "$schema" (never over
			// schema_map). $schema itself is never synced.
			schema_from_file: true
			// On: a file its schema refuses is not written, and a file no schema checks
			// fails too, unless schema_map maps it to "none". Off (the default): the
			// failure is logged and the file is written.
			strict_validation: true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
