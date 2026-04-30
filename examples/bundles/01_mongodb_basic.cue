// Example: Single MongoDB sink — minimal setup.
// The bundle is the live config loaded by all VarTrack services.
// Push it to the orchestrator with: POST /v1/bundles
bundle: {
	platforms: [
		{
			github: {
				endpoint:        "https://github.com"
				protocol:        "https"
				token:           "ghp_xxxxxxxxxxxxxxxxxxxx"
				org_name:        "my-org"
				verify_ssl:      true
				timeout:         "30s"
				max_retries:     3
				push_event_name: "push"
			}
		},
	]

	datasources: [
		{
			mongo: {
				tag:             ""
				endpoint:        "mongodb://mongo-host:27017"
				host:            "mongo-host"
				port:            27017
				database:        "vartrack"
				collection:      "app_vars"
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

	// One rule: push from the github platform writes to the "mongo" datasource.
	// Webhook URL: POST /v1/webhooks/mongo
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
