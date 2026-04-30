// Example: MongoDB with authentication, TLS, and replica set.
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
			mongo: {
				tag:      ""
				endpoint: "mongodb://app-user:app-pass@mongo-host:27017/vartrack?authSource=admin&replicaSet=rs0"
				host:     "mongo-host"
				port:     27017

				// Credentials for MongoDB auth.
				username: "app-user"
				password: "app-pass"

				database:        "vartrack"
				collection:      "app_vars"
				auth_source:     "admin"
				update_strategy: "STRATEGY_KEY_VALUE"

				// TLS — provide your CA cert file path in the container.
				ssl:                            true
				ssl_allow_invalid_certificates: false

				// Tuning knobs.
				buffer_size:        200
				max_pool_size:      10
				connect_timeout_ms: 10000
			}
		},
		{
			redis: {
				tag:      "redis"
				hosts:    ["redis-host:6379"]
				password: "redis-pass"
				database: 0
			}
		},
		{
			redis: {
				tag:      "redis-results"
				hosts:    ["redis-host:6379"]
				password: "redis-pass"
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
