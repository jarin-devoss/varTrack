// Variation 6: Dual MongoDB datasources (primary + DR) with @secret injection.
// Two mongo sinks of the same type, differentiated by tag:
//   mongo-primary → collection primary_vars
//   mongo-dr      → collection dr_vars
// Both rules use Vault to resolve @secret fields (database.password, api.secret_key).
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
				tag:             "primary"
				endpoint:        "mongodb://mongo:27017"
				host:            "mongo"
				port:            27017
				database:        "vartrack"
				collection:      "primary_vars"
				update_strategy: "STRATEGY_KEY_VALUE"
				max_pool_size:   5
			}
		},
		{
			mongo: {
				tag:             "dr"
				endpoint:        "mongodb://mongo:27017"
				host:            "mongo"
				port:            27017
				database:        "vartrack"
				collection:      "dr_vars"
				update_strategy: "STRATEGY_KEY_VALUE"
				max_pool_size:   5
			}
		},
		{
			redis: {
				tag:      "redis"
				hosts:    ["redis:6379"]
				database: 0
			}
		},
		{
			redis: {
				tag:      "redis-results"
				hosts:    ["redis:6379"]
				database: 1
			}
		},
	]

	rules: [
		{
			platform:             "github"
			datasource:           "mongo-primary"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        false
			sync_mode:            "SYNC_MODE_FULL"
			prune:                {last: false}
			self_heal:            false
			destination_template: "primary-{env}"
			secrets: {
				default: {
					type:         "vault"
					vault_addr:   "http://vault:8200"
					auth: {type: "token", token: "root"}
					mount_point:  "secret"
					path_prefix:  "vartrack/configs"
					kv_version:   2
				}
			}
		},
		{
			platform:             "github"
			datasource:           "mongo-dr"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        false
			sync_mode:            "SYNC_MODE_FULL"
			prune:                {last: false}
			self_heal:            false
			destination_template: "dr-{env}"
			secrets: {
				default: {
					type:         "vault"
					vault_addr:   "http://vault:8200"
					auth: {type: "token", token: "root"}
					mount_point:  "secret"
					path_prefix:  "vartrack/configs"
					kv_version:   2
				}
			}
		},
	]

	schema_registry: {
		platform: "github"
		repo:     "vartrack-demo/schemas"
		branch:   "main"
	}

	global_tags: {
		team:                "platform"
		environment:         "demo"
		celery_broker:       "redis"
		celery_backend:      "redis-results"
		gateway_nonce_redis: "redis"
		watcher_state_redis: "redis"
	}
}
