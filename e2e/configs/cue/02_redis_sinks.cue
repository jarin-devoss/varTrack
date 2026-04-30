// Variation 2: Redis as primary data sinks (HASH on DB2, STRING on DB3)
// Celery broker stays on DB0, result backend on DB1.
// destination_template sets the key prefix per env.
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
			// Primary data sink: Redis HASH, DB 2
			redis: {
				tag:            "redis-hash-sink"
				hosts:          ["redis:6379"]
				database:       2
				data_structure: "HASH"
			}
		},
		{
			// Secondary data sink: Redis STRING, DB 3
			redis: {
				tag:            "redis-string-sink"
				hosts:          ["redis:6379"]
				database:       3
				data_structure: "STRING"
			}
		},
		{
			// Celery broker
			redis: {
				tag:      "redis"
				hosts:    ["redis:6379"]
				database: 0
			}
		},
		{
			// Celery result backend
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
			datasource:           "redis-hash-sink"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        true
			sync_mode:            "SYNC_MODE_FULL"
			prune:                true
			self_heal:            false
			destination_template: "{env}:config"
		},
		{
			platform:             "github"
			datasource:           "redis-string-sink"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        false
			sync_mode:            "SYNC_MODE_FULL"
			self_heal:            false
			destination_template: "vartrack:kv"
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
