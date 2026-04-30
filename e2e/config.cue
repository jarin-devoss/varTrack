// VarTrack E2E demo bundle.
// Gitea (GitHub-compatible) as git platform, MongoDB + ZooKeeper as sinks.
bundle: {
	platforms: [
		{
			github: {
				endpoint:          "http://gitea:3000"
				protocol:          "http"
				token:             "demo-admin-token"
				org_name:          "vartrack-demo"
				verify_ssl:        false
				timeout:           "30s"
				max_retries:       3
				event_type_header: "X-GitHub-Event"
				push_event_name:   "push"
				pr_event_name:     "pull_request"
			}
		},
	]

	datasources: [
		{
			mongo: {
				tag:                            ""
				endpoint:                       "mongodb://mongo:27017"
				host:                           "mongo"
				port:                           27017
				database:                       "vartrack"
				collection:                     "variables"
				env_as_collection:              false
				auth_source:                    "admin"
				ssl:                            false
				ssl_allow_invalid_certificates: false
				buffer_size:                    100
				max_pool_size:                  5
				connect_timeout_ms:             5000
				update_strategy:                "STRATEGY_KEY_VALUE"
			}
		},
		{
			zookeeper: {
				tag:             ""
				hosts:           ["zookeeper:2181"]
				session_timeout: "10s"
			}
		},
		{
			// Primary MongoDB sink — key-value strategy, collection: primary_vars.
			mongo: {
				tag:                            "primary"
				endpoint:                       "mongodb://mongo:27017"
				host:                           "mongo"
				port:                           27017
				database:                       "vartrack"
				collection:                     "primary_vars"
				env_as_collection:              false
				auth_source:                    "admin"
				ssl:                            false
				ssl_allow_invalid_certificates: false
				buffer_size:                    100
				max_pool_size:                  5
				connect_timeout_ms:             5000
				update_strategy:                "STRATEGY_KEY_VALUE"
			}
		},
		{
			// DR MongoDB sink — same instance, separate collection: dr_vars.
			mongo: {
				tag:                            "dr"
				endpoint:                       "mongodb://mongo:27017"
				host:                           "mongo"
				port:                           27017
				database:                       "vartrack"
				collection:                     "dr_vars"
				env_as_collection:              false
				auth_source:                    "admin"
				ssl:                            false
				ssl_allow_invalid_certificates: false
				buffer_size:                    100
				max_pool_size:                  5
				connect_timeout_ms:             5000
				update_strategy:                "STRATEGY_KEY_VALUE"
			}
		},
		{
			// Celery broker + gateway nonce store + watcher state store.
			// DB 0 is used for tasks and ephemeral state.
			redis: {
				tag:      "broker"
				hosts:    ["redis:6379"]
				database: 0
			}
		},
		{
			// Celery result backend uses a separate DB to avoid key collisions
			// with task queue entries.
			redis: {
				tag:      "results"
				hosts:    ["redis:6379"]
				database: 1
			}
		},
	]

	rules: [
		{
			platform:   "github"
			datasource: "mongo"
			self_heal:  true
		},
		{
			platform:   "github"
			datasource: "zookeeper"
			self_heal:  true
		},
	]

	// ── Infrastructure wiring ────────────────────────────────────────────────
	// Each value is the datasource name ({type}-{tag}) that plays that role.
	// Matches the dedicated fields added in bundle.proto (fields 10–13).
	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-results"
	gateway_nonce_datasource:  "redis-broker"
	watcher_state_datasource:  "redis-broker"

	global_tags: {
		team:        "platform"
		environment: "demo"
	}
}
