// Example: destination_template — write to a different collection/path per environment.
//
// HOW IT WORKS:
//   The settings.json pushed to the orchestrator contains a `destination_template`
//   field. The template supports {env} and {branch} placeholders resolved at
//   ETL time from the Git event.
//
//   MongoDB example:
//     destination_template: "app-vars-{env}"
//     branch "main"   → collection "app-vars-production"
//     branch "staging"→ collection "app-vars-staging"
//
//   ZooKeeper example:
//     destination_template: "/configs/{env}/app"
//     → znode: /configs/production/app  or  /configs/staging/app
//
// This allows a single rule to fan out to environment-specific destinations
// without defining one rule per environment.
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
				// Base collection — overridden per-push by destination_template in settings.
				collection:      "app_vars"
				update_strategy: "STRATEGY_KEY_VALUE"
			}
		},
		{
			zookeeper: {
				tag:             ""
				hosts:           ["zk1:2181", "zk2:2181"]
				session_timeout: "15s"
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
		},
		{
			platform:   "github"
			datasource: "zookeeper"
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

// SETTINGS FILE (pushed to POST /v1/bundles/{tenant}/rules):
// {
//   "rules": [
//     {
//       "platform": "github",
//       "datasource": "mongo",
//       "destination_template": "app-vars-{env}",
//       "branch_env_map": {
//         "main":    "production",
//         "staging": "staging",
//         "dev":     "development"
//       }
//     }
//   ]
// }
