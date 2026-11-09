// Example: Full production bundle — GitHub + Gitea platforms, MongoDB primary/DR,
// ZooKeeper, Redis config store, Vault secrets, multiple environments.
//
// Architecture:
//   GitHub (main org)  → POST /v1/webhooks/mongo-primary  → MongoDB primary AZ-A
//                      → POST /v1/webhooks/mongo-dr       → MongoDB DR AZ-B
//                      → POST /v1/webhooks/zookeeper      → ZooKeeper quorum
//   Gitea (internal)   → POST /v1/webhooks/redis-cfg      → Redis config DB
bundle: {
	platforms: [
		{
			github: {
				endpoint:    "https://github.com"
				protocol:    "https"
				token:       "ghp_xxxxxxxxxxxxxxxxxxxx"
				org_name:    "acme-corp"
				timeout:     "30s"
				max_retries: 3
			}
		},
		{
			// Internal Gitea instance.
			gitea: {
				endpoint:    "https://gitea.acme.internal"
				protocol:    "https"
				token:       "gta_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
				org_name:    "platform-team"
				timeout:     "30s"
				max_retries: 3
			}
		},
	]

	datasources: [
		{
			// Primary MongoDB — AZ-A.
			mongo: {
				tag:             "primary"
				endpoint:        "mongodb://app-user:pass@mongo-az-a.acme.internal:27017/vartrack?authSource=admin"
				database:        "vartrack"
				collection:      "app_vars"
				auth_source:     "admin"
				ssl:             true
				update_strategy: "STRATEGY_KEY_VALUE"
				max_pool_size:   20
			}
		},
		{
			// DR MongoDB — AZ-B.
			mongo: {
				tag:             "dr"
				endpoint:        "mongodb://app-user:pass@mongo-az-b.acme.internal:27017/vartrack?authSource=admin"
				database:        "vartrack"
				collection:      "app_vars_dr"
				auth_source:     "admin"
				ssl:             true
				update_strategy: "STRATEGY_KEY_VALUE"
				max_pool_size:   10
			}
		},
		{
			// ZooKeeper quorum — 3 nodes for HA.
			zookeeper: {
				tag: ""
				hosts: ["zk1.acme.internal:2181", "zk2.acme.internal:2181", "zk3.acme.internal:2181"]
				session_timeout: "15s"
			}
		},
		{
			// Redis config store (Gitea rules write here).
			redis: {
				tag: "redis-cfg"
				hosts: ["redis-config.acme.internal:6379"]
				password: "redis-cfg-pass"
				database: 2
			}
		},
		{
			// Celery broker.
			redis: {
				tag: "redis"
				hosts: ["redis-broker.acme.internal:6379"]
				password: "redis-broker-pass"
				database: 0
			}
		},
		{
			// Celery result backend.
			redis: {
				tag: "redis-results"
				hosts: ["redis-broker.acme.internal:6379"]
				password: "redis-broker-pass"
				database: 1
			}
		},
	]

	// Vault, for @secret() fields and {ref: {path, key}} credentials,
	// logged in with the pod's Kubernetes service account.
	secret_managers: [
		{
			vault: {
				tag:         "default"
				endpoint:    "https://vault.acme.internal:8200"
				mount_point: "secret"
				kv_version:  2
				kubernetes_auth: {role: "vartrack-orchestrator"}
			}
		},
	]

	rules: [
		{
			platform:   "github"
			datasource: "mongo-primary"
			file_name:  "app.yaml"
			repositories: ["acme-corp/configs"]
			// Where the environments come from: a rule that names none is refused.
			branch_map: {main: "production"}
			self_heal: true
			// Production never prunes, whatever the tenant's bundle.json or a
			// repo's vartrack.json says: force is applied after both.
			force: {prune: {enabled: false}}
		},
		{
			platform:   "github"
			datasource: "mongo-dr"
			file_name:  "app.yaml"
			repositories: ["acme-corp/configs"]
			// Where the environments come from: a rule that names none is refused.
			branch_map: {main: "production"}
			self_heal: true
		},
		{
			platform:   "github"
			datasource: "zookeeper"
			file_name:  "app.yaml"
			repositories: ["acme-corp/configs"]
			// Where the environments come from: a rule that names none is refused.
			branch_map: {main: "production"}
			self_heal: true
		},
		{
			platform:   "gitea"
			datasource: "redis-cfg"
			file_name:  "app.yaml"
			repositories: ["acme-corp/configs"]
			// Where the environments come from: a rule that names none is refused.
			branch_map: {main: "production"}
			self_heal: false // No drift healing for Redis config — push-only.
		},
	]

	global_tags: {
		team:        "platform"
		environment: "production"
		region:      "us-east-1"

		celery_broker:       "redis"
		celery_backend:      "redis-results"
		gateway_nonce_redis: "redis"
		watcher_state_redis: "redis"
	}
}
