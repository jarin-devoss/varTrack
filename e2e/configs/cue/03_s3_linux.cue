// Variation 3: S3 (MinIO) + Linux server file sinks
// Useful for testing file-based config distribution.
// destination_template sets the S3 key prefix and remote SSH file path.
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
