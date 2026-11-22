// Example 05: S3 (or MinIO, or any S3-compatible store) as the sink.
//
// With STRATEGY_FILE each config file is stored as a file, in its own format and
// under its own name, below the rule's destination_template:
//   s3://my-config-bucket/acme/production/app.yaml
// (byte for byte as in Git, unless a sync changed it: an environment taken out,
// a root_key, a secret, a renamed key). The other strategies:
//   STRATEGY_DOCUMENT    <prefix>/config.json, nested
//   STRATEGY_KEY_VALUE   one object per key: <prefix>/database.host
//   unset                <prefix>/config.json with the flat keys
// Without a destination_template the prefix is the datasource's `prefix` + "/<env>".
//
// Webhook: https://<gateway>/webhooks/s3
bundle: {
	platforms: [
		{
			github: {
				endpoint: "https://github.com"
				secret:   "my-webhook-secret"
				token:    "ghp_xxxxxxxxxxxxxxxxxxxx"
			}
		},
	]

	datasources: [
		{
			s3: {
				bucket: "my-config-bucket"
				region: "us-east-1"

				// Leave both keys out to use the AWS default credential chain
				// (an instance role, IRSA). Any credential form works here.
				access_key_id:     {file: "/run/secrets/aws-access-key-id"}
				secret_access_key: {file: "/run/secrets/aws-secret-access-key"}

				// MinIO or another S3-compatible store:
				// endpoint:         "http://minio:9000"
				// force_path_style: true

				update_strategy: "STRATEGY_FILE"
			}
		},
		{
			redis: {
				tag:   "broker"
				hosts: ["redis:6379"]
				database: 0
			}
		},
	]

	rules: [
		{
			platform:     "github"
			datasource:   "s3"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			destination_template: "{tenant}/{env}/"
			self_heal:            true
			// file_format: "json"   // convert: the object becomes app.json
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
