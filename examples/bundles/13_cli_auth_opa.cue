// Example 13 — Who may use the CLI API: Git permissions + OPA
//
// The CLI API (/v1/cli/*, used by vt and the web UI's Playground) asks the
// rule's Git platform what the caller may do, with the caller's own Git
// token: push access to the rule's repositories syncs, read access
// validates, dry-runs and reads tasks. ui.auth.admins may do anything. The
// OPA policy below then decides by environment and file path.
//
//   vt login --server https://gateway.example.com --token <your GitHub token>
bundle: {
	platforms: [
		{
			github: {
				endpoint:        "https://github.example.com"
				token:           "ghp_xxxxxxxxxxxxxxxxxxxx"
				secret:          "my-webhook-secret"
				push_event_name: "push"
			}
		},
	]

	datasources: [
		{
			mongo: {
				tag:        "primary"
				endpoint:   "mongodb://mongo:27017"
				database:   "vartrack"
				collection: "app_vars"
			}
		},
	]

	rules: [
		{
			platform:     "github"
			datasource:   "mongo-primary"
			repositories: ["acme/app-configs"]
			file_name:    "configs/app.yaml"
			root_key:     "app"
		},
	]

	auth: {
		opa: {
			// Inline Rego — compiled in-process at gateway startup (~50µs eval, no extra process).
			// Same policy as examples/policies/vartrack_authz.rego:
			//   - varTrack admins and repository admins sync anywhere
			//   - writers sync outside production, but not configs/secrets/*
			//   - dry runs anywhere (nothing is written)
			inline_policy: """
				package vartrack.authz

				default allow = false

				# validate and task reads: Git read access has decided
				allow {
					input.action != "sync"
				}

				allow {
					input.action == "sync"
					is_admin
				}

				allow {
					input.action == "sync"
					input.user.permission == "write"
					not startswith(input.env, "prod")
					not startswith(input.file_path, "configs/secrets/")
				}

				allow {
					input.action == "sync"
					input.dry_run == true
				}

				is_admin {
					input.user.admin == true
				}

				is_admin {
					input.user.permission == "admin"
				}
			"""

			// To use a file instead (reload without restart via /admin/opa/reload):
			// policy_file: "/etc/vartrack/policy.rego"

			// To delegate to a remote OPA server:
			// server_url: "http://opa:8181/v1/data/vartrack/authz/allow"
		}
	}
}
