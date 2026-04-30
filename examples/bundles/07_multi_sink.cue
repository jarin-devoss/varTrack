// Example: Multiple sinks — same push writes to MongoDB, ZooKeeper, and Redis.
// Each datasource has its own rule and webhook path:
//   POST /v1/webhooks/mongo      → writes to MongoDB
//   POST /v1/webhooks/zookeeper  → writes to ZooKeeper
//   POST /v1/webhooks/redis-cfg  → writes to Redis
//
// A single GitHub webhook can call all three in sequence, or you can route
// different repos/branches to different sinks via separate rules.
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
				tag:             ""
				endpoint:        "mongodb://mongo-host:27017"
				database:        "vartrack"
				collection:      "app_vars"
				update_strategy: "STRATEGY_KEY_VALUE"
			}
		},
		{
			zookeeper: {
				tag:             ""
				hosts:           ["zk1:2181", "zk2:2181", "zk3:2181"]
				session_timeout: "15s"
			}
		},
		{
			redis: {
				tag:      "redis-cfg"
				hosts:    ["redis-host:6379"]
				database: 2
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
		{
			platform:   "github"
			datasource: "redis-cfg"
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
