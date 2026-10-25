// Example: web UI sign-in with two Git platforms.
//
// Users sign in to the varTrack web UI with GitHub or Gitea and connect the
// other to the same session. Each rule is judged by the account on the rule's
// own platform: a rule is shown when that account can read one of the rule's
// repositories, and can be synced when it can write to one.
//
// OAUTH APPLICATIONS (one per platform, all with the same callback):
//   Callback URL: https://vartrack.example.com/api/orchestrator/v1/auth/callback
//   GitHub: Settings → Developer settings → OAuth Apps → New OAuth App
//   Gitea:  Settings → Applications → OAuth2 Applications (confidential client)
//
// Without the ui.auth block the UI is open to anyone who can reach it.
bundle: {
	platforms: [
		{
			github: {
				endpoint: "https://github.com"
				protocol: "https"
				token:    {file: "/run/secrets/github-token"}
			}
		},
		{
			gitea: {
				tag:      "internal"                 // → platform name "gitea-internal"
				endpoint: "http://gitea:3000"         // as varTrack reaches it
				protocol: "http"
				token:    {file: "/run/secrets/gitea-token"}
			}
		},
	]

	datasources: [
		{
			mongo: {
				tag:        "primary"
				endpoint:   "mongodb://mongo:27017"
				database:   "vartrack"
				collection: "variables"
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

	// Two platforms write to mongo-primary, one rule each: a rule is identified
	// by its platform and datasource. A webhook to /webhooks/mongo-primary is
	// told apart by the headers of the platform that sent it (X-Gitea-Event,
	// X-GitHub-Event), and `vt sync` / `vt validate` take --platform to say
	// which rule they mean.
	rules: [
		{
			platform:     "github"
			datasource:   "mongo-primary"
			file_name:    "config/app.yaml"
			repositories: ["acme/*"]
			branch_map: {main: "production", develop: "staging"}
		},
		{
			platform:     "gitea-internal"
			datasource:   "mongo-primary"
			file_name:    "config/app.yaml"
			repositories: ["platform/infra-config"]
			branch_map: {main: "production"}
		},
	]

	schema_registry: {
		platform: "github"
		repo:     "acme/vartrack-schemas"
		branch:   "main"
	}

	// Where {ref: {path, key}} values below are read from.
	secret_managers: [
		{
			vault: {
				endpoint:    "https://vault.acme.example"
				mount_point: "secret"
				kv_version:  2
				kubernetes_auth: {role: "vartrack"}
			}
		},
	]

	ui: {
		public_url: "https://vartrack.example.com"
		auth: {
			providers: [
				{
					platform:      "github"
					client_id:     "Iv1.8a61f9b3a7aba766"           // public: fine inline
					client_secret: {file: "/run/secrets/github-oauth"}
				},
				{
					platform:      "gitea-internal"
					client_id:     {file: "/run/secrets/gitea-oauth-id"}
					// From Vault: field client_secret of secret/ui/gitea.
					client_secret: {ref: {path: "ui/gitea", key: "client_secret"}}
					// The platform's endpoint above is internal; this is the address
					// users' browsers reach for the consent page.
					browser_url: "https://gitea.acme.example"
				},
			]
			// "platform:login" — these see and sync every rule.
			admins: ["github:octo-admin"]
			session_ttl_seconds: 28800 // 8 hours
			// The secret manager {ref: …} values come from. Optional here: there is only one.
			secret_manager: "vault"
		}
	}

	celery_broker_datasource: "redis-broker"
}
