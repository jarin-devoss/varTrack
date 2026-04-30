// Example: Full production bundle — GitHub + GitLab platforms, MongoDB primary/DR,
// ZooKeeper, Redis config store, Vault secrets, multiple environments.
//
// Architecture:
//   GitHub (main org)  → POST /v1/webhooks/mongo-primary  → MongoDB primary AZ-A
//                      → POST /v1/webhooks/mongo-dr       → MongoDB DR AZ-B
//                      → POST /v1/webhooks/zookeeper      → ZooKeeper quorum
//   GitLab (internal)  → POST /v1/webhooks/redis-cfg      → Redis config DB
bundle: {
	platforms: [
		{
			github: {
				endpoint:    "https://github.com"
				protocol:    "https"
				token:       "ghp_xxxxxxxxxxxxxxxxxxxx"
				org_name:    "acme-corp"
				verify_ssl:  true
				timeout:     "30s"
				max_retries: 3
			}
		},
		{
			// Internal GitLab instance.
			github: {
				endpoint:          "https://gitlab.acme.internal"
				protocol:          "https"
				token:             "glpat-xxxxxxxxxxxxxxxxxxxx"
				org_name:          "platform-team"
				verify_ssl:        true
				timeout:           "30s"
				max_retries:       3
				event_type_header: "X-Gitlab-Event"
				push_event_name:   "Push Hook"
				pr_event_name:     "Merge Request Hook"
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
				tag:             ""
				hosts:           ["zk1.acme.internal:2181", "zk2.acme.internal:2181", "zk3.acme.internal:2181"]
				session_timeout: "15s"
			}
		},
		{
			// Redis config store (GitLab rules write here).
			redis: {
				tag:      "redis-cfg"
				hosts:    ["redis-config.acme.internal:6379"]
				password: "redis-cfg-pass"
				database: 2
			}
		},
		{
			// Celery broker.
			redis: {
				tag:      "redis"
				hosts:    ["redis-broker.acme.internal:6379"]
				password: "redis-broker-pass"
				database: 0
			}
		},
		{
			// Celery result backend.
			redis: {
				tag:      "redis-results"
				hosts:    ["redis-broker.acme.internal:6379"]
				password: "redis-broker-pass"
				database: 1
			}
		},
	]

	rules: [
		{
			platform:   "github"
			datasource: "mongo-primary"
			self_heal:  true
			vault: {
				address: "https://vault.acme.internal:8200"
				auth_method: "kubernetes"
				role:    "vartrack-orchestrator"
			}
		},
		{
			platform:   "github"
			datasource: "mongo-dr"
			self_heal:  true
			vault: {
				address: "https://vault.acme.internal:8200"
				auth_method: "kubernetes"
				role:    "vartrack-orchestrator"
			}
		},
		{
			platform:   "github"
			datasource: "zookeeper"
			self_heal:  true
		},
		{
			platform:   "github"  // GitLab platform is also declared as "github" type.
			datasource: "redis-cfg"
			self_heal:  false     // No drift healing for Redis config — push-only.
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
