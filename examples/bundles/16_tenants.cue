// Example 16: two tenants sharing one Redis, each limited to its own keys.
//
// A tenant is a team you declare. A push belongs to the tenant whose `repositories`
// it came from (a push no tenant owns is refused, 403). Each tenant has its own
// schema repository: its bundle.json (its layer of the rules), its CUE schemas and
// its presets. A push to that repository refreshes that tenant and no other.
//
// The one rule below covers both tenants' repositories, and writes to
// "{tenant}:{env}": payments:production, search:production. Each tenant may write
// only its `destinations`, so a sync that would land outside them is refused
// before the sink is opened.
//
// The tenants' bundle.json files: examples/layers/bundle.json is one.
bundle: {
	platforms: [
		{
			github: {
				endpoint: "https://github.com"
				secret:   "my-webhook-secret"
				token:    "ghp_xxxxxxxxxxxxxxxxxxxx" // reads the config repositories of both tenants
			}
		},
	]

	datasources: [
		{
			redis: {
				tag:      "config"
				hosts:    ["redis:6379"]
				database: 2
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

	tenants: [
		{
			name:         "payments"
			repositories: ["payments/*"]
			schema_repo: {
				platform: "github"
				repo:     "payments/schemas"
				branch:   "main"
				token:    {file: "/run/secrets/payments-schemas-token"} // read access to it
			}
			destinations: [{datasource: "redis-config", paths: ["payments:*"]}]
			// Presets may also come from these repositories (besides its own ones).
			presets: ["platform/presets"]
			presets_token: {file: "/run/secrets/platform-presets-token"}
		},
		{
			name:         "search"
			repositories: ["search/app", "search/infra"]
			schema_repo: {
				platform: "github"
				repo:     "search/schemas"
				branch:   "main"
				token:    {file: "/run/secrets/search-schemas-token"}
			}
			destinations: [{datasource: "redis-config", paths: ["search:*"]}]
		},
	]

	rules: [
		{
			platform:     "github"
			datasource:   "redis-config"
			repositories: ["payments/*", "search/app", "search/infra"]
			// The tenants whose bundle.json may layer this rule (empty: any).
			tenants:    ["payments", "search"]
			file_name:  "configs/app.yaml"
			branch_map: {main: "production"}
			destination_template: "{tenant}:{env}"
			self_heal:            true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
