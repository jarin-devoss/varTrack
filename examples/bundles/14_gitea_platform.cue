// Example 14: Gitea as the Git platform.
//
// Gitea sends GitHub-shaped payloads with its own headers:
//   X-Gitea-Event      push / pull_request
//   X-Gitea-Signature  HMAC-SHA256 of the body, hex, no "sha256=" prefix
//   X-Gitea-Delivery   a delivery id (replay protection)
//
// WEBHOOK SETUP IN GITEA
//   Settings → Webhooks → Add Webhook → Gitea
//   Target URL:   https://<gateway>/webhooks/mongo
//   Content type: application/json
//   Secret:       the platform's `secret` below
//   Trigger on:   Push Events (and Pull Request Events with pr_as_env)
// Gitea's test delivery is answered 200 "event ignored": it is not a push.
bundle: {
	platforms: [
		{
			gitea: {
				endpoint:    "https://gitea.mycompany.com"
				token:       "gta_xxxxxxxxxxxxxxxxxxxxxxxxxxxx" // personal access token: read the repositories
				secret:      "my-webhook-secret"                // HMAC secret of the webhook
				org_name:    "my-org"
				timeout:     "30s"
				max_retries: 3
				page_size:   50
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
			redis: {
				tag:      "broker"
				hosts:    ["redis:6379"]
				database: 0
			}
		},
		{
			redis: {
				tag:      "results"
				hosts:    ["redis:6379"]
				database: 1
			}
		},
	]

	rules: [
		{
			platform:             "gitea"
			datasource:           "mongo"
			repositories:         ["my-org/*"]
			file_name:            "configs/app.yaml"
			destination_template: "{env}-config"
			sync_mode:            "AUTO"
			self_heal:            true
			branch_map: {
				main:    "production"
				develop: "staging"
			}
		},
	]

	// The wiring is at the top of the bundle, not in global_tags.
	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-results"
	gateway_nonce_datasource:  "redis-broker"
}
