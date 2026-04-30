// Example: ZooKeeper sink — stores config as znodes under /vartrack/{env}/{key}.
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
			zookeeper: {
				// No tag → datasource name is "zookeeper".
				tag: ""

				// ZooKeeper quorum: list all nodes for HA.
				hosts: ["zk1:2181", "zk2:2181", "zk3:2181"]

				// Session timeout — disconnected writes are buffered until reconnect.
				session_timeout: "15s"
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

	// Webhook: POST /v1/webhooks/zookeeper
	rules: [
		{
			platform:   "github"
			datasource: "zookeeper"
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
