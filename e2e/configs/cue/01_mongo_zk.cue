// Variation 1: MongoDB + ZooKeeper
// Base setup: key-value store in MongoDB, hierarchical config in ZooKeeper.
// destination_template drives the MongoDB collection name and ZK root path.
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
				tag:             "mongo-kv"
				endpoint:        "mongodb://mongo:27017"
				host:            "mongo"
				port:            27017
				database:        "vartrack"
				collection:      "variables"
				update_strategy: "STRATEGY_KEY_VALUE"
				max_pool_size:   5
			}
		},
		{
			zookeeper: {
				tag:             "zk-main"
				hosts:           ["zookeeper:2181"]
				session_timeout: "10s"
				key_format:      "slash"
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
			datasource:           "mongo-kv"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        true
			sync_mode:            "SYNC_MODE_FULL"
			prune:                {last: false}
			self_heal:            true
			destination_template: "{env}-vars"
		},
		{
			platform:             "github"
			datasource:           "zk-main"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        false
			sync_mode:            "SYNC_MODE_FULL"
			prune:                {last: false}
			self_heal:            true
			destination_template: "/vartrack/demo/default"
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
