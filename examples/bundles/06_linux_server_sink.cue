// Example: Linux server (SSH) sink — writes rendered config files to a remote Linux host.
// Useful for deploying .env files, app.conf, nginx.conf, etc. directly to servers.
bundle: {
	platforms: [
		{
			github: {
				endpoint:    "https://github.com"
				protocol:    "https"
				token:       "ghp_xxxxxxxxxxxxxxxxxxxx"
				org_name:    "my-org"
				verify_ssl:  true
				timeout:     "30s"
				max_retries: 3
			}
		},
	]

	datasources: [
		{
			linux_server: {
				// No tag → datasource name is "linux_server".
				tag:  ""
				host:     "app-server-01.internal"
				port:     22
				username: "deploy"

				// Password auth — prefer an SSH key in production.
				password: "deploy-password"

				// Or use an SSH key (one or the other, not both):
				// ssh_key: {private_key: {file: "/secrets/id_rsa"}}

				// The file written, relative to base_directory; with env_as_path
				// each environment gets its own directory: /etc/vartrack/{env}/app.env.
				base_directory: "/etc/vartrack"
				file_paths:     ["app.env"]
				env_as_path:    true
			}
		},
		{
			redis: {
				tag:      "redis"
				hosts:    ["redis-host:6379"]
				database: 0
			}
		},
		{
			redis: {
				tag:      "redis-results"
				hosts:    ["redis-host:6379"]
				database: 1
			}
		},
	]

	// Webhook: POST /v1/webhooks/linux_server
	rules: [
		{
			platform:   "github"
			datasource: "linux_server"
			self_heal:  true
		},
	]

	global_tags: {
		team:        "platform"
		environment: "production"

		celery_broker:       "redis"
		celery_backend:      "redis-results"
		gateway_nonce_redis: "redis"
		watcher_state_redis: "redis"
	}
}
