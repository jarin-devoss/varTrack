// Example: Vault @secret annotations.
//
// HOW IT WORKS:
//   Any config file in the Git repo can reference a Vault secret using the
//   CUE @secret("path#field") annotation. Before writing to the sink, the
//   orchestrator resolves the annotation by calling Vault's KV API and
//   substitutes the real value.
//
//   This keeps secrets OUT of Git while still allowing Git-driven config management.
//
// CONFIG FILE IN GIT (app.yaml):
//   database:
//     host: prod-db.internal
//     password: "@secret(secret/myapp/db#password)"
//   api:
//     key: "@secret(secret/myapp/api#key)"
//
// VAULT SETUP:
//   vault kv put secret/myapp/db     password=supersecret123
//   vault kv put secret/myapp/api    key=api-key-xyz789
//
// The written config (in MongoDB / ZooKeeper / etc.) will contain the real values.
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

			// Vault integration — resolver is applied to @secret() annotations.
			vault: {
				address:    "http://vault.internal:8200"
				token:      "s.xxxxxxxxxxxxxxxxxxxxxxxx"
				// Or use AppRole auth:
				// auth_method: "approle"
				// role_id:     "my-role-id"
				// secret_id:   "my-secret-id"
			}
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
