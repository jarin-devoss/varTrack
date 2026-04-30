// Example: Gitea as the Git platform.
//
// Gitea is a self-hosted Git service with a GitHub-compatible API.
// varTrack supports Gitea webhooks natively using the X-Gitea-* header set.
//
// WEBHOOK SETUP IN GITEA:
//   Settings → Webhooks → Add Webhook → Gitea
//   Payload URL:  http://gateway:5657/webhooks/mongo
//   Content type: application/json
//   Secret:       (same as bundle secret below)
//   Trigger:      Push Events + Pull Request Events
//
// GITEA WEBHOOK HEADERS (fixed by Gitea, no configuration needed):
//   X-Gitea-Event      → event type ("push" / "pull_request")
//   X-Gitea-Signature  → HMAC-SHA256 of the payload (hex, no prefix)
//   X-Gitea-Delivery   → unique delivery UUID (replay protection)
bundle: {
	platforms: [
		{
			gitea: {
				endpoint:    "https://gitea.mycompany.com"
				protocol:    "https"
				token:       "gta_xxxxxxxxxxxxxxxxxxxxxxxxxxxx" // Gitea personal access token
				secret:      "my-webhook-secret"               // HMAC secret for signature verification
				org_name:    "my-org"
				verify_ssl:  true
				timeout:     "30s"
				max_retries: 3
				page_size:   50
			}
		},
	]

	datasources: [
		{
			mongo: {
				endpoint:   "mongodb://mongo:27017"
				database:   "vartrack"
				collection: "app_vars"
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
			file_name:            "configs/app.yaml"
			repositories:         ["my-org/*"]
			destination_template: "{env}-config"
			sync_mode:            "AUTO"
			self_heal:            true
			branch_map: {
				main:    "production"
				develop: "staging"
			}
			prune: {
				last:    true
				dry_run: false
			}
		},
	]

	global_tags: {
		celery_broker_datasource:  "redis-broker"
		celery_backend_datasource: "redis-results"
	}
}
