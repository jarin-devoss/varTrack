// Example 07: one file into three sinks: MongoDB, ZooKeeper and Redis.
//
// A rule is named by its platform and datasource, so each sink has its own rule
// and its own webhook URL. Register one webhook per datasource in the repository:
//   https://<gateway>/webhooks/mongo
//   https://<gateway>/webhooks/zookeeper
//   https://<gateway>/webhooks/redis-config
// Each delivery is synced on its own; one failing does not stop the others.
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
			mongo: {
				endpoint:        "mongodb://mongo:27017"
				database:        "vartrack"
				collection:      "variables"
				update_strategy: "STRATEGY_DOCUMENT"
			}
		},
		{
			zookeeper: {
				hosts: ["zk1:2181", "zk2:2181", "zk3:2181"]
			}
		},
		{
			// tag "config" → "redis-config". (A tag is added to the type: tag
			// "redis-config" would be named "redis-redis-config".)
			redis: {
				tag:      "config"
				hosts:    ["redis:6379"]
				database: 2
			}
		},
		{
			redis: {
				tag:      "broker"
				hosts:    ["redis:6379"]
				database: 0
			}
		},
	]

	rules: [
		{
			platform:             "github"
			datasource:           "mongo"
			repositories:         ["acme-corp/configs"]
			file_name:            "configs/app.yaml"
			branch_map:           {main: "production", develop: "staging"}
			destination_template: "{env}-config"
			self_heal:            true
		},
		{
			platform:             "github"
			datasource:           "zookeeper"
			repositories:         ["acme-corp/configs"]
			file_name:            "configs/app.yaml"
			branch_map:           {main: "production", develop: "staging"}
			destination_template: "/myapp/{env}"
			self_heal:            true
		},
		{
			platform:             "github"
			datasource:           "redis-config"
			repositories:         ["acme-corp/configs"]
			file_name:            "configs/app.yaml"
			branch_map:           {main: "production", develop: "staging"}
			destination_template: "{env}:cfg"
			self_heal:            true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
