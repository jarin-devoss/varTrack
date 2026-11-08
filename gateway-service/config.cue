// config.cue — Minimal local development bundle.
//
// Usage:
//   go run ./cmd/...
//   curl -X POST http://127.0.0.1:5657/webhooks/mongo \
//     -H "Content-Type: application/json" \
//     -H "X-GitHub-Event: push" \
//     -d '{"ref":"refs/heads/main","repository":{"full_name":"my-org/my-repo"}}'

bundle: {
	platforms: [{
		github: {
			endpoint:          "https://github.com"
			protocol:          "https"
			token:             "ghp_PLACEHOLDER_TOKEN"
			org_name:          "my-org"
			timeout:           "30s"
			max_retries:       3
			page_size:         30
			event_type_header: "X-GitHub-Event"
			git_scm_signature: "X-Hub-Signature-256"
			push_event_name:   "push"
			pr_event_name:     "pull_request"
		}
	}]

	datasources: [{
		mongo: {
			endpoint:          "mongodb://localhost:27017"
			host:              "localhost"
			port:              27017
			database:          "vartrack"
			collection:        "configs"
			update_strategy:   1
			auth_source:       "admin"
			ssl:               false
			buffer_size:       100
			max_pool_size:     10
			connect_timeout_ms: 5000
			ssl_allow_invalid_certificates: false
		}
	}]

	rules: [{
		platform:     "github"
		datasource:   "mongo"
		file_name:    "vartrack.yaml"
		repositories: ["my-org/*"]
	}]

	schema_registry: {
		platform: "github"
		repo:     "my-org/schemas"
		branch:   "main"
	}

	// The orchestrator's own settings (broker, schema and git caches) are its environment variables, not part
	// of the bundle: docs/reference/environment-variables.md.
}
