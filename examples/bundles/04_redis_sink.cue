// Example 04: Redis as the sink, in its own database next to the Celery broker.
//
// A push to main writes configs/app.yaml as the fields of one hash:
//   HSET production:cfg database.host "prod-db.internal"
// The hash is the rule's destination_template; without one it is the datasource's
// key_prefix, else "<tenant>:<datasource>:<env>" (default:redis-config:production).
//
// Webhook: https://<gateway>/webhooks/redis-config
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
			// The config store: tag "config" → the datasource is named "redis-config".
			redis: {
				tag:      "config"
				hosts:    ["redis:6379"]
				database: 2
				data_structure: "HASH" // the default; "STRING" is one key per setting
				// Without these a Redis that hangs leaves the worker waiting.
				connection_timeout: "5s"
				read_timeout:       "5s"
			}
		},
		{
			// The Celery broker and the replay store, in database 0.
			redis: {
				tag:      "broker"
				hosts:    ["redis:6379"]
				database: 0
			}
		},
	]

	rules: [
		{
			platform:     "github"
			datasource:   "redis-config"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			destination_template: "{env}:cfg"
			self_heal:            true
			prune: {
				enabled:    true
				protection: ["runtime.*"] // keys another tool owns: never deleted
			}
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
