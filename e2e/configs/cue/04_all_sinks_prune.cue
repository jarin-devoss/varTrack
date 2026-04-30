// Variation 4: All 5 sink types enabled simultaneously with prune + self_heal.
// Tests that all sinks can run in parallel and that prune correctly removes
// stale keys after a config file change.
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
				tag:            "redis-data"
				hosts:          ["redis:6379"]
				database:       2
				data_structure: "HASH"
			}
		},
		{
			s3: {
				tag:          "s3-minio"
				bucket:       "vartrack"
				region:       "us-east-1"
				endpoint_url: "http://minio:9002"
				access_key:   "minioadmin"
				secret_key:   "minioadmin"
			}
		},
		{
			linux_server: {
				tag:      "linux-target"
				host:     "linux-target"
				port:     22
				user:     "vartrack"
				password: "VarTrack123"
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
			prune:                {last: true}
			self_heal:            true
			destination_template: "{env}-vars"
		},
		{
			platform:             "github"
			datasource:           "zk-main"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        true
			sync_mode:            "SYNC_MODE_FULL"
			prune:                true
			self_heal:            true
			destination_template: "/vartrack/demo/{env}"
		},
		{
			platform:             "github"
			datasource:           "redis-data"
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
			datasource:           "s3-minio"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        true
			sync_mode:            "SYNC_MODE_FULL"
			self_heal:            false
			destination_template: "configs/{env}"
		},
		{
			platform:             "github"
			datasource:           "linux-target"
			file_name:            "app-settings.yaml"
			repositories:         ["vartrack-demo/demo-configs"]
			env_as_branch:        true
			sync_mode:            "SYNC_MODE_FULL"
			self_heal:            false
			destination_template: "/tmp/vartrack/{env}.env"
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
