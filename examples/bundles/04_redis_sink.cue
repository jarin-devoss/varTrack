// Example: Redis as both the config sink AND the Celery broker.
// Redis sink stores variables as HASH fields under key "vartrack:{env}".
bundle: {
	platforms: [
		{
			github: {
				endpoint:    "https://github.com"
				protocol:    "https"
				token:       "ghp_xxxxxxxxxxxxxxxxxxxx"
				org_name:    "my-org"
				verify_ssl:  true
				timeout:     "30s"
				max_retries: 3
			}
		},
	]

	datasources: [
		{
			// Redis config sink — tagged "config" so the datasource name is "redis-config".
			redis: {
				tag:      "config"
				hosts:    ["redis-host:6379"]
				database: 2
			}
		},
		{
			// Celery broker — separate DB to avoid collisions.
			redis: {
				tag:      "redis"
				hosts:    ["redis-host:6379"]
				database: 0
			}
		},
		{
			// Celery result backend.
			redis: {
				tag:      "redis-results"
				hosts:    ["redis-host:6379"]
				database: 1
			}
		},
	]

	// Webhook: POST /v1/webhooks/redis-config
	rules: [
		{
			platform:   "github"
			datasource: "redis-config"
			self_heal:  true
		},
	]

	global_tags: {
		team:        "platform"
		environment: "production"

		celery_broker:       "redis"
		celery_backend:      "redis-results"
		gateway_nonce_redis: "redis"
		watcher_state_redis: "redis"
	}
}
