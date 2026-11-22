// Example 06: a Linux server over SSH as the sink: the config is written as a file.
//
// With env_as_path each environment gets its own directory:
//   /etc/myapp/production/app.env
// The file is written to a temp file next to the target, checked by
// validation_command, and moved into place in one rename, so a reader sees the
// old file or the new one, never half of either.
//
// Webhook: https://<gateway>/webhooks/linux_server
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
			// No tag → the datasource is named "linux_server".
			linux_server: {
				host:     "app-server-01.internal"
				port:     22
				username: "deploy"

				// The SSH key is read from a file of its own. (Or a password, in any
				// credential form: password: {file: "/run/secrets/ssh-password"}.)
				ssh_key: {private_key: {file: "/run/secrets/deploy-key"}}

				// The host's key must already be in this file ("strict", the default).
				// "accept-new" records a host seen for the first time; never use "off".
				host_key_verification: "strict"
				known_hosts_file:      "/etc/vartrack/known_hosts"

				base_directory: "/etc/myapp"
				file_paths:     ["app.env"] // the file written, under base_directory
				env_as_path:    true

				file_mode:           "0640"
				backup_before_write: true
				post_write_commands: ["systemctl reload myapp"]
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
			datasource:   "linux_server"
			repositories: ["acme-corp/configs"]
			// A YAML file in Git, written as KEY=value lines: the file's extension
			// (app.env) sets the format. A rule's destination_template, when set,
			// is the full path instead ("/etc/myapp/{env}.env").
			file_name: "configs/app.yaml"
			branch_map: {main: "production"}
			self_heal: true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
