// Example 01: one GitHub platform, one MongoDB sink. The smallest bundle that runs.
//
// Every service reads this file at CONFIG_PATH (config.cue). Check it with:
//   vartrack-bundle check config.cue
//
// Register the webhook in each repository (or the organisation):
//   Payload URL:  https://<gateway>/webhooks/mongo   (the datasource's name)
//   Content type: application/json
//   Secret:       the platform's `secret` below
//   Events:       Pushes
bundle: {
	platforms: [
		{
			github: {
				endpoint:        "https://github.com"
				push_event_name: "push"
				pr_event_name:   "pull_request"
				secret:          "my-webhook-secret"        // the webhook's HMAC secret
				token:           "ghp_xxxxxxxxxxxxxxxxxxxx" // reads the config repositories
			}
		},
	]

	datasources: [
		{
			// No tag: the datasource is named "mongo".
			mongo: {
				endpoint:   "mongodb://mongo:27017"
				database:   "vartrack"
				collection: "variables"
				// One nested document per environment and file. Set it on purpose:
				// changing it later replaces the stored documents.
				update_strategy: "STRATEGY_DOCUMENT"
			}
		},
		{
			// The Celery broker and result backend, and the gateway's replay store.
			redis: {
				tag:   "broker" // → "redis-broker"
				hosts: ["redis:6379"]
				database: 0
			}
		},
	]

	rules: [
		{
			platform:     "github"
			datasource:   "mongo"
			repositories: ["acme-corp/*"]   // whose pushes this rule syncs
			file_name:    "configs/app.yaml" // the file it reads, at the pushed commit
			// Where the environments come from. A rule that names none is refused,
			// and a push to a branch that is not mapped is skipped.
			branch_map: {
				main:    "production"
				develop: "staging"
			}
			destination_template: "{env}-config" // collection production-config, staging-config
			self_heal:            true            // the watcher writes Git's values back on drift
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
