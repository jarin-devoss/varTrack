// varTrack live demo bundle — every sink that runs in a container
// (Vercel is a hosted service, so it is the one sink left out).
//
// Gitea is the Git platform. One config repo,
// vartrack-demo/demo-configs, feeds every sink: pushes to `main` land in
// production, pushes to `develop` land in staging.
//
// bundle.json in vartrack-demo/schemas carries the same rules for the
// orchestrator workers (with sink connection settings inline); keep the two
// in sync.
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
	]

	datasources: [
		{
			mongo: {
				tag:             ""
				endpoint:        "mongodb://mongo:27017"
				host:            "mongo"
				port:            27017
				database:        "vartrack"
				collection:      "variables"
				auth_source:     "admin"
				max_pool_size:   5
				update_strategy: "STRATEGY_KEY_VALUE"
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
			linux_server: {
				tag:      ""
				host:     "linux-target"
				port:     22
				username: "vartrack"
				password: {value: "VarTrack123"}
			}
		},
		{
			// k3s in a container; live-seed's kubeconfig points at https://k3s:6443.
			configmap: {
				tag:             ""
				kubeconfig_path: "/live-state/kubeconfig"
				namespace:       "default"
				configmap_name:  "checkout-api"
			}
		},
		{
			helm: {
				tag:             ""
				kubeconfig_path: "/live-state/kubeconfig"
				namespace:       "default"
				release_name:    "checkout-api"
				chart_name:      "/charts/checkout-api"
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
	]

	rules: [
		{
			platform:   "gitea"
			datasource: "mongo"
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
			datasource: "linux_server"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode:            "SYNC_MODE_AUTO"
			self_heal:            false
			destination_template: "/home/vartrack/app/{env}.env"
		},
		{
			platform:   "gitea"
			datasource: "configmap"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode:            "SYNC_MODE_AUTO"
			self_heal:            true
			destination_template: "checkout-api-{env}"
		},
		{
			platform:   "gitea"
			datasource: "helm"
			file_name:  "app-settings.yaml"
			repositories: ["vartrack-demo/demo-configs"]
			branch_map: {main: "production", develop: "staging"}
			sync_mode:            "SYNC_MODE_AUTO"
			self_heal:            false
			destination_template: "checkout-api-{env}"
		},
	]

	schema_registry: {
		platform: "gitea"
		repo:     "vartrack-demo/schemas"
		branch:   "main"
	}

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-results"
	gateway_nonce_datasource:  "redis-broker"
	watcher_state_datasource:  "redis-broker"

	global_tags: {
		team:        "platform"
		environment: "demo"
	}
}
