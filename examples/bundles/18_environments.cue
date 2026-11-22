// Example 18: where a push's environment comes from, and one file for every environment.
//
// The first strategy that places the push wins:
//   1. branch_map      main → production
//   2. file_path_map   (see example 17)
//   3. pr_as_env       PR #42 → pr-42
//   4. branch_as_env   feature/login → feature/login
//   5. tag_as_env      tag v1.2.3 → v1.2.3
//   6. none: the push feeds no environment of the rule and is skipped (there is no
//      "default" environment; branch_map: {main: "default"} makes one of that name)
//
// The file can hold the settings of every environment
// (examples/configs/settings.multi-env.yaml):
//   config:
//     default:    {database: {host: db.internal, port: 5432}}
//     production: {database: {host: db.prod.internal}}
// A push to main writes database.host=db.prod.internal, database.port=5432; a PR,
// whose pr-42 the file does not name, gets default's settings.
bundle: {
	platforms: [
		{
			github: {
				endpoint:        "https://github.com"
				push_event_name: "push"
				pr_event_name:   "pull_request" // send Pull requests to the webhook too
				secret:          "my-webhook-secret"
				token:           "ghp_xxxxxxxxxxxxxxxxxxxx"
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

	rules: [
		{
			platform:     "github"
			datasource:   "redis-config"
			repositories: ["acme-corp/checkout"]
			file_name:    "configs/settings.multi-env.yaml"
			root_key:     "config" // only this block; other_tool is never synced
			branch_map: {
				main:    "production"
				develop: "staging"
			}
			pr_as_env:  true // preview environments, one per pull request
			tag_as_env: true // a release tag is its own environment
			destination_template: "checkout:{env}"
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
