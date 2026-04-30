// Example: S3 sink — uploads rendered config files to an S3 bucket.
// Works with AWS S3 and any S3-compatible store (MinIO, Ceph, etc.).
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
			s3: {
				tag:    ""
				bucket: "my-config-bucket"
				region: "us-east-1"

				// Credentials — leave empty to use EC2 instance role or ECS task role.
				access_key: "AKIAIOSFODNN7EXAMPLE"
				secret_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

				// Optional: custom endpoint for MinIO / on-prem S3.
				// endpoint: "http://minio:9002"
				// force_path_style: true

				// Key prefix: files are uploaded as s3://bucket/prefix/{env}/{filename}.
				prefix: "vartrack/"
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

	// Webhook: POST /v1/webhooks/s3
	rules: [
		{
			platform:   "github"
			datasource: "s3"
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
