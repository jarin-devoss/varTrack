// Example 08: two datasources of the same type, a primary and a DR copy.
//
// The `tag` tells them apart and names them:
//   tag ""         → "mongo"
//   tag "primary"  → "mongo-primary"
//   tag "dr"       → "mongo-dr"
// The name is the webhook URL and what a rule's `datasource` says. Register both:
//   https://<gateway>/webhooks/mongo-primary
//   https://<gateway>/webhooks/mongo-dr
// Each is synced, and checked for drift, on its own.
//
// The same holds for two destinations in one database: a rule is named by its
// platform and datasource, so a second rule needs a second (tagged) datasource.
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
				tag:             "primary"
				endpoint:        "mongodb://mongo-primary.internal:27017"
				database:        "vartrack"
				collection:      "variables"
				update_strategy: "STRATEGY_DOCUMENT"
			}
		},
		{
			mongo: {
				tag:             "dr"
				endpoint:        "mongodb://mongo-dr.internal:27017"
				database:        "vartrack"
				collection:      "variables"
				update_strategy: "STRATEGY_DOCUMENT"
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
			datasource:   "mongo-primary"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production"}
			self_heal: true
		},
		{
			platform:     "github"
			datasource:   "mongo-dr"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production"}
			self_heal: true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
