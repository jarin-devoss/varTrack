// Example 03: ZooKeeper as the sink: one znode per key.
//
// A push to main writes configs/app.yaml's keys under
//   /vartrack/<tenant>/production/<key>      e.g. /vartrack/default/production/database.host
// base_path (the datasource's) comes first, then the rule's destination_template
// ("/{tenant}/{env}" when the rule sets none).
//
// Webhook: https://<gateway>/webhooks/zookeeper
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
			// No tag → the datasource is named "zookeeper".
			zookeeper: {
				hosts:     ["zk1:2181", "zk2:2181", "zk3:2181"] // the whole quorum
				base_path: "/vartrack"
				// A znode per key is the only shape ZooKeeper stores: update_strategy is
				// STRATEGY_KEY_VALUE or unset.
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
			datasource:   "zookeeper"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			destination_template: "/{tenant}/{env}"
			self_heal:            true
			// ZooKeeper keeps each key on its own, so prune can delete the keys Git no
			// longer holds (only the ones varTrack wrote).
			prune: true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
	// The watcher's leader lock; ZooKeeper works as well as Redis.
	watcher_leader_election_datasource: "zookeeper"
}
