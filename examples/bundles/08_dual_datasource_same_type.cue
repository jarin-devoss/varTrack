// Example: Two datasources of the same type (primary + DR).
//
// HOW IT WORKS:
//   The `tag` field makes each datasource unique. The datasource name used in
//   webhook URLs is constructed as:
//
//     tag == ""      → name is the type:  "mongo"
//     tag != ""      → name is "type-tag": "mongo-primary", "mongo-dr"
//
//   So each datasource gets its own webhook endpoint:
//     POST /v1/webhooks/mongo-primary
//     POST /v1/webhooks/mongo-dr
//
//   You can call both from a single GitHub webhook by registering two
//   webhook URLs, or trigger them independently.
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
			// Primary MongoDB — production writes go here first.
			mongo: {
				tag:             "primary"
				endpoint:        "mongodb://mongo-primary.internal:27017"
				database:        "vartrack"
				collection:      "app_vars"
				update_strategy: "STRATEGY_KEY_VALUE"
			}
		},
		{
			// DR MongoDB — disaster-recovery replica in a separate availability zone.
			// Same schema, different host + collection to allow independent drift detection.
			mongo: {
				tag:             "dr"
				endpoint:        "mongodb://mongo-dr.internal:27017"
				database:        "vartrack"
				collection:      "app_vars_dr"
				update_strategy: "STRATEGY_KEY_VALUE"
			}
		},
		{
			redis: {
				tag:      "redis"
				hosts:    ["redis-host:6379"]
				database: 0
			}
		},
		{
			redis: {
				tag:      "redis-results"
				hosts:    ["redis-host:6379"]
				database: 1
			}
		},
	]

	// Two rules — one per datasource.
	rules: [
		{
			platform:   "github"
			datasource: "mongo-primary"
			self_heal:  true
		},
		{
			platform:   "github"
			datasource: "mongo-dr"
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
