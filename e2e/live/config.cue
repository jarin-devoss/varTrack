// varTrack live demo bundle — MongoDB, ZooKeeper, Redis, S3, and Kubernetes
// ConfigMaps and Helm on k3s (Vercel, a hosted service, and the Linux server
// sink are left out).
//
// Gitea is the Git platform. One config repo,
// vartrack-demo/demo-configs, feeds every sink: pushes to `main` land in
// production, pushes to `develop` land in staging.
//
// The rules here are the first layer. A tenant's bundle.json lays its rules
// over them — vartrack-demo/schemas for tenant "default", payments/schemas on
// gitea-team for tenant "payments" (the redis-team rule) — and a config
// repo's vartrack.json goes over both. bundle.json only holds what it adds
// (key_format, helm_values_mode, ...); connection settings and secrets come
// from this file only. Both "extends" presets from vartrack-demo/presets (see
// e2e/live/presets).
bundle: {
	platforms: [
		{
			gitea: {
				endpoint:    "http://gitea:3000"
				protocol:    "http"
				org_name:    "vartrack-demo"
				verify_ssl:  false
				timeout:     "30s"
				max_retries: 3
			}
		},
		{
			// A second Git server: the payments team's. Its rules are judged by
			// the account users connect there, not by their gitea account.
			gitea: {
				tag:         "team"
				endpoint:    "http://gitea-team:3000"
				protocol:    "http"
				org_name:    "payments"
				verify_ssl:  false
				timeout:     "30s"
				max_retries: 3
			}
		},
	]

	datasources: [
		{
			mongo: {
				tag:             "primary"
				endpoint:        "mongodb://mongo:27017"
				host:            "mongo"
				port:            27017
				database:        "vartrack"
				collection:      "variables"
				auth_source:     "admin"
				max_pool_size:   5
				update_strategy: "STRATEGY_DOCUMENT"
			}
		},
		{
			zookeeper: {
				tag: ""
				hosts: ["zookeeper:2181"]
				session_timeout: "10s"
				base_path:       "/vartrack"
			}
		},
		{
			// Config values (db 2). The broker and results use db 0 and 1.
			redis: {
				tag: ""
				hosts: ["redis:6379"]
				database: 2
			}
		},
		{
			// S3-compatible object store: MinIO in a container.
			s3: {
				tag:              "minio"
				bucket:           "vartrack"
				region:           "us-east-1"
				endpoint:         "http://minio:9002"
				force_path_style: true
				access_key_id: {value: "minioadmin"}
				secret_access_key: {value: "minioadmin"}
			}
		},
		{
			// k3s in a container; live-seed's kubeconfig points at https://k3s:6443.
			configmap: {
				tag:             "checkout"
				kubeconfig_path: "/live-state/kubeconfig"
				namespace:       "default"
				configmap_name:  "checkout-api"
			}
		},
		{
			helm: {
				tag:              "checkout"
				kubeconfig_path:  "/live-state/kubeconfig"
				namespace:        "default"
				release_name:     "checkout-api"
				chart_name:       "/charts/checkout-api"
				upgrade_strategy: "INSTALL_OR_UPGRADE"
			}
		},
		{
			redis: {
				tag: "broker"
				hosts: ["redis:6379"]
				database: 0
			}
		},
		{
			redis: {
				tag: "results"
				hosts: ["redis:6379"]
				database: 1
			}
		},
		{
			// The payments team's settings, fed from gitea-team.
			redis: {
				tag: "team"
				hosts: ["redis:6379"]
				database: 4
			}
		},
	]

	rules: [
		{
			platform:   "gitea"
			datasource: "mongo-primary"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode: "SYNC_MODE_AUTO"
			prune: {enabled: true}
			self_heal: true
		},
		{
			platform:   "gitea"
			datasource: "zookeeper"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode: "SYNC_MODE_AUTO"
			prune: {enabled: true}
			self_heal:            true
			destination_template: "/vartrack/{tenant}/{env}"
		},
		{
			platform:   "gitea"
			datasource: "redis"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode: "SYNC_MODE_AUTO"
			prune: {enabled: true}
			self_heal:            false
			destination_template: "{env}:config"
		},
		{
			platform:   "gitea"
			datasource: "s3-minio"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode:            "SYNC_MODE_AUTO"
			self_heal:            false
			destination_template: "configs/{tenant}/{env}"
		},
		{
			platform:   "gitea"
			datasource: "configmap-checkout"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode:            "SYNC_MODE_AUTO"
			self_heal:            true
			destination_template: "checkout-api-{env}"
		},
		{
			platform:   "gitea"
			datasource: "helm-checkout"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode:            "SYNC_MODE_AUTO"
			self_heal:            false
			destination_template: "checkout-api-{env}"
		},
		{
			platform:   "gitea-team"
			datasource: "redis-team"
			// Only the payments tenant's bundle.json may layer this rule.
			tenants: ["payments"]
			file_name:  "app-settings.yaml"
			repositories: ["payments/payments-config"]
			branch_map: {main: "production"}
			sync_mode:            "SYNC_MODE_AUTO"
			self_heal:            false
			destination_template: "{env}:payments"
		},
	]

	schema_registry: {
		platform: "gitea"
		repo:     "vartrack-demo/schemas"
		branch:   "main"
	}

	// The demo Vault (dev mode, root token). {ref: {path, key}} values are
	// read from it: secret/data/{path}, field {key}.
	secret_managers: [
		{
			vault: {
				endpoint:    "http://vault:8200"
				mount_point: "secret"
				kv_version:  2
				token_auth: {token: "root"}
			}
		},
	]

	// Web UI: where users open it, and sign-in with both Gitea servers. Each
	// user sees the rules whose repositories their own account on the rule's
	// platform can read. live-seed registers an OAuth application on each
	// server: gitea's credentials go to files in /live-state/ui-sign-in,
	// gitea-team's client secret to Vault.
	// Remove auth for an open UI.
	ui: {
		public_url: "http://localhost:5173"
		auth: {
			providers: [
				{
					platform: "gitea"
					client_id: {file: "/live-state/ui-sign-in/gitea-client-id"}
					client_secret: {file: "/live-state/ui-sign-in/gitea-client-secret"}
					browser_url: "http://localhost:3000"
				},
				{
					platform: "gitea-team"
					client_id: {file: "/live-state/ui-sign-in/gitea-team-client-id"}
					client_secret: {ref: {path: "ui/gitea-team", key: "client_secret"}}
					// 127.0.0.1, not localhost: browsers share cookies across ports,
					// and both Gitea servers name their session cookie the same.
					browser_url: "http://127.0.0.1:3001"
				},
			]
			// Where {ref: …} values come from. With one secret manager it could be left out.
			secret_manager: "vault"
		}
	}

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
