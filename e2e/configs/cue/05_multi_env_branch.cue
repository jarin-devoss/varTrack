// Variation 5: Multi-environment routing via branch_map.
// main → "production", develop → "staging", feature/* → env_as_branch.
// destination_template places each env in its own MongoDB collection and ZK path.
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
			mongo: {
				tag:             "mongo-file"
				endpoint:        "mongodb://mongo:27017"
				host:            "mongo"
				port:            27017
				database:        "vartrack"
				collection:      "snapshots"
				update_strategy: "STRATEGY_FILE"
				max_pool_size:   5
			}
		},
		{
			zookeeper: {
				tag:             "zk-main"
				hosts:           ["zookeeper:2181"]
				session_timeout: "10s"
				key_format:      "slash_dot"
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
			// Key-value store: collection named after the env
			platform:             "github"
			datasource:           "mongo-kv"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        true
			sync_mode:            "SYNC_MODE_GIT_SMART_REPAIR"
			prune:                {last: true}
			self_heal:            true
			destination_template: "{env}-config"
			branch_map: {
				main:    "production"
				develop: "staging"
			}
		},
		{
			// File snapshot: one document per file per env
			platform:             "github"
			datasource:           "mongo-file"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        true
			sync_mode:            "SYNC_MODE_FULL"
			prune:                {last: true}
			self_heal:            false
			destination_template: "{env}-snapshots"
			branch_map: {
				main:    "production"
				develop: "staging"
			}
		},
		{
			// ZooKeeper: paths scoped per env
			platform:             "github"
			datasource:           "zk-main"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        true
			sync_mode:            "SYNC_MODE_FULL"
			prune:                true
			self_heal:            true
			destination_template: "/apps/demo/{env}"
			branch_map: {
				main:    "production"
				develop: "staging"
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
