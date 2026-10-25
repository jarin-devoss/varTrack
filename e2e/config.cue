// VarTrack E2E demo bundle.
// Gitea (GitHub-compatible) as git platform, MongoDB + ZooKeeper as sinks.
//
// The rules here are the first layer; schemas/bundle.json (tenant "demo")
// lays its rules over them — file, branch and sync settings — and the
// datasources' connection settings come from this file only.
bundle: {
	platforms: [
		{
			github: {
				endpoint:          "http://gitea:3000"
				protocol:          "http"
				token:             "demo-admin-token"
				org_name:          "vartrack-demo"
				verify_ssl:        false
				timeout:           "30s"
				max_retries:       3
				event_type_header: "X-GitHub-Event"
				push_event_name:   "push"
				pr_event_name:     "pull_request"
			}
		},
	]

	datasources: [
		{
			mongo: {
				tag:                            ""
				endpoint:                       "mongodb://mongo:27017"
				host:                           "mongo"
				port:                           27017
				database:                       "vartrack"
				collection:                     "variables"
				env_as_collection:              false
				auth_source:                    "admin"
				ssl:                            false
				ssl_allow_invalid_certificates: false
				buffer_size:                    100
				max_pool_size:                  5
				connect_timeout_ms:             5000
				update_strategy:                "STRATEGY_KEY_VALUE"
			}
		},
		{
			zookeeper: {
				tag:             ""
				hosts:           ["zookeeper:2181"]
				session_timeout: "10s"
			}
		},
		{
			// Primary MongoDB sink — key-value strategy, collection: primary_vars.
			mongo: {
				tag:                            "primary"
				endpoint:                       "mongodb://mongo:27017"
				host:                           "mongo"
				port:                           27017
				database:                       "vartrack"
				collection:                     "primary_vars"
				env_as_collection:              false
				auth_source:                    "admin"
				ssl:                            false
				ssl_allow_invalid_certificates: false
				buffer_size:                    100
				max_pool_size:                  5
				connect_timeout_ms:             5000
				update_strategy:                "STRATEGY_KEY_VALUE"
			}
		},
		{
			// DR MongoDB sink — same instance, separate collection: dr_vars.
			mongo: {
				tag:                            "dr"
				endpoint:                       "mongodb://mongo:27017"
				host:                           "mongo"
				port:                           27017
				database:                       "vartrack"
				collection:                     "dr_vars"
				env_as_collection:              false
				auth_source:                    "admin"
				ssl:                            false
				ssl_allow_invalid_certificates: false
				buffer_size:                    100
				max_pool_size:                  5
				connect_timeout_ms:             5000
				update_strategy:                "STRATEGY_KEY_VALUE"
			}
		},
		{
			// Document-strategy MongoDB sink — one MongoDB document per synced file, always nested.
			mongo: {
				tag:                            "document"
				endpoint:                       "mongodb://mongo:27017"
				host:                           "mongo"
				port:                           27017
				database:                       "vartrack"
				collection:                     "documents"
				env_as_collection:              false
				auth_source:                    "admin"
				ssl:                            false
				ssl_allow_invalid_certificates: false
				buffer_size:                    100
				max_pool_size:                  5
				connect_timeout_ms:             5000
				update_strategy:                "STRATEGY_DOCUMENT"
			}
		},
		{
			// File-strategy MongoDB sink — one MongoDB document per synced file, the file as it parsed.
			mongo: {
				tag:                            "file"
				endpoint:                       "mongodb://mongo:27017"
				host:                           "mongo"
				port:                           27017
				database:                       "vartrack"
				collection:                     "files"
				env_as_collection:              false
				auth_source:                    "admin"
				ssl:                            false
				ssl_allow_invalid_certificates: false
				buffer_size:                    100
				max_pool_size:                  5
				connect_timeout_ms:             5000
				update_strategy:                "STRATEGY_FILE"
			}
		},
		{
			// Redis sink for the HASH suites: one hash per environment, database 2.
			redis: {
				tag:            "hash"
				hosts:          ["redis:6379"]
				database:       2
				data_structure: "HASH"
			}
		},
		{
			// Redis sink for the STRING suite: one string per key, database 3.
			redis: {
				tag:            "string"
				hosts:          ["redis:6379"]
				database:       3
				data_structure: "STRING"
			}
		},
		{
			// SSH file sink: the rule's destination_template names the file.
			linux_server: {
				tag:            ""
				host:           "linux-target"
				port:           22
				username:       "vartrack"
				password:       "VarTrack123"
				base_directory: "/tmp/vartrack"
				file_paths:     ["app.env"]
				// The target is a throwaway container with a host key generated at every
				// start; strict checking (the default) would refuse it. accept-new
				// records the key in a file outside any volume, so the next stack
				// (new key) starts clean.
				host_key_verification: "accept-new"
				known_hosts_file:      "/tmp/vt_known_hosts"
			}
		},
		{
			// S3-compatible store (RustFS, which keeps the name "minio").
			s3: {
				tag:               ""
				endpoint:          "http://minio:9002"
				region:            "us-east-1"
				bucket:            "vartrack"
				access_key_id:     "minioadmin"
				secret_access_key: "minioadmin"
				force_path_style:  true
			}
		},
		{
			// Celery broker + gateway nonce store + watcher state store.
			// DB 0 is used for tasks and ephemeral state.
			redis: {
				tag:      "broker"
				hosts:    ["redis:6379"]
				database: 0
			}
		},
		{
			// Celery result backend uses a separate DB to avoid key collisions
			// with task queue entries.
			redis: {
				tag:      "results"
				hosts:    ["redis:6379"]
				database: 1
			}
		},
	]

	// Vault, for the @secret() fields of the CUE schema (database.password and
	// api.secret_key). A field the config file leaves as a plain value is read at
	// path_prefix, under the field's own name. Rules name no Vault of their own.
	secret_managers: [
		{
			vault: {
				endpoint:    "http://vault:8200"
				mount_point: "secret"
				kv_version:  2
				path_prefix: "vartrack/configs"
				token_auth: {token: "root"}
			}
		},
	]

	rules: [
		{
			platform:   "github"
			datasource: "mongo"
			repositories: ["vartrack-demo/demo-configs"]
			self_heal:    true
		},
		{
			platform:   "github"
			datasource: "zookeeper"
			repositories: ["vartrack-demo/demo-configs"]
			self_heal:    true
		},
		{
			platform:   "github"
			datasource: "mongo-document"
			repositories: ["vartrack-demo/demo-configs"]
		},
		{
			platform:   "github"
			datasource: "mongo-file"
			repositories: ["vartrack-demo/demo-configs"]
		},
		{
			platform:   "github"
			datasource: "redis-hash"
			repositories: ["vartrack-demo/demo-configs"]
		},
		{
			platform:   "github"
			datasource: "redis-string"
			repositories: ["vartrack-demo/demo-configs"]
		},
		{
			platform:   "github"
			datasource: "linux_server"
			repositories: ["vartrack-demo/demo-configs"]
		},
		{
			platform:   "github"
			datasource: "s3"
			repositories: ["vartrack-demo/demo-configs"]
		},
		{
			platform:   "github"
			datasource: "mongo-primary"
			repositories: ["vartrack-demo/demo-configs"]
		},
		{
			platform:   "github"
			datasource: "mongo-dr"
			repositories: ["vartrack-demo/demo-configs"]
		},
	]

	// ── Infrastructure wiring ────────────────────────────────────────────────
	// Each value is the datasource name ({type}-{tag}) that plays that role.
	// Matches the dedicated fields in bundle.proto (fields 10–12).
	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-results"
	gateway_nonce_datasource:  "redis-broker"

	global_tags: {
		team:        "platform"
		environment: "demo"
	}

	// CLI API (vt, the web UI's Playground): callers use their own Gitea
	// token, and may do what it may do in the rule's repositories; the
	// example OPA policy adds: writers never sync production or
	// configs/secrets/*.
	auth: {
		opa: {
			policy_file: "/app/policies/vartrack_authz.rego"
		}
	}
}
