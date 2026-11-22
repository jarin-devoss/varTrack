// Example 12: a production bundle — two Git platforms, MongoDB primary and DR,
// ZooKeeper, a Redis config store, Vault, and the settings production needs.
//
//   GitHub  acme-corp/configs     → /webhooks/mongo-primary, /webhooks/mongo-dr, /webhooks/zookeeper
//   Gitea   platform-team/configs → /webhooks/redis-config
//
// Run every service with APP_ENV=production, TLS and INTERNAL_AUTH_SECRET (see the
// production checklist). With APP_ENV=production the gateway refuses to start
// without gateway_nonce_datasource, and checks every webhook's signature against
// its platform's `secret`.
//
// No credential is written here: {file: …} is a mounted secret, read on the machine
// of the service that uses it; {ref: …} is read from Vault.
bundle: {
	platforms: [
		{
			github: {
				endpoint: "https://github.com"
				org_name: "acme-corp"
				secret:   {file: "/run/secrets/github-webhook-secret"}
				token:    {ref: {path: "vartrack/github", key: "token"}}
			}
		},
		{
			gitea: {
				endpoint: "https://gitea.acme.internal"
				// Its own secret: two platforms are never told apart by a shared one.
				secret: {file: "/run/secrets/gitea-webhook-secret"}
				token:  {ref: {path: "vartrack/gitea", key: "token"}}
			}
		},
	]

	datasources: [
		{
			mongo: {
				tag:                "primary"
				host:               "mongo-az-a.acme.internal"
				username:           "vartrack"
				password:           {ref: {path: "vartrack/mongo", key: "password"}}
				database:           "vartrack"
				collection:         "variables"
				update_strategy:    "STRATEGY_DOCUMENT"
				ssl:                true
				ssl_ca_path:        "/etc/ssl/acme-ca.pem"
				connect_timeout_ms: 5000
			}
		},
		{
			mongo: {
				tag:                "dr"
				host:               "mongo-az-b.acme.internal"
				username:           "vartrack"
				password:           {ref: {path: "vartrack/mongo", key: "password"}}
				database:           "vartrack"
				collection:         "variables"
				update_strategy:    "STRATEGY_DOCUMENT"
				ssl:                true
				ssl_ca_path:        "/etc/ssl/acme-ca.pem"
				connect_timeout_ms: 5000
			}
		},
		{
			zookeeper: {
				hosts:     ["zk1.acme.internal:2181", "zk2.acme.internal:2181", "zk3.acme.internal:2181"]
				base_path: "/vartrack"
			}
		},
		{
			redis: {
				tag:                "config"
				hosts:              ["redis-config.acme.internal:6379"]
				password:           {ref: {path: "vartrack/redis-config", key: "password"}}
				database:           0
				connection_timeout: "5s"
				read_timeout:       "5s"
			}
		},
		{
			// The broker, the sync ledger, the locks, the replay store and the
			// watcher's lock. Run it with persistence (AOF): prune and drift work from
			// what it holds. Its password is a {file: …}: the watcher cannot read Vault.
			redis: {
				tag:      "broker"
				hosts:    ["redis-broker.acme.internal:6379"]
				password: {file: "/run/secrets/redis-broker-password"}
				database: 0
			}
		},
	]

	secret_managers: [
		{
			vault: {
				tag:         "default"
				endpoint:    "https://vault.acme.internal:8200"
				mount_point: "secret"
				kv_version:  2
				kubernetes_auth: {role: "vartrack-orchestrator"}
			}
		},
	]

	schema_registry: {
		platform: "github"
		repo:     "acme-corp/vartrack-schemas"
		branch:   "main"
	}

	rules: [
		{
			platform:     "github"
			datasource:   "mongo-primary"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			self_heal: true
			// Pinned: no tenant bundle.json or repository vartrack.json can change these.
			force: {
				prune:             {enabled: false}
				strict_validation: true
			}
		},
		{
			platform:     "github"
			datasource:   "mongo-dr"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production"}
			self_heal: true
			force: {
				prune:             {enabled: false}
				strict_validation: true
			}
		},
		{
			platform:     "github"
			datasource:   "zookeeper"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			self_heal:         true
			strict_validation: true
			prune: {enabled: true, protection: ["_vt_*", "runtime.*"]}
		},
		{
			platform:     "gitea"
			datasource:   "redis-config"
			repositories: ["platform-team/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production"}
			destination_template: "{env}:cfg"
			self_heal:            true
			// Keys the application changes itself: shown, never drift, never healed.
			drift_ignore: ["feature_flags.rollout_*"]
		},
	]

	// How many syncs one worker container runs at once. A machine can say otherwise
	// with VARTRACK_WORKER_CONCURRENCY. The number of containers is the Deployment's
	// replicas (or docker compose --scale orchestrator-worker=N), not set here.
	workers: {
		concurrency:         8
		max_tasks_per_child: 500
	}

	// How many syncs may write to a store at once, across all workers.
	sync_limits: [
		{name: "mongo-az-a", datasources: ["mongo-primary"], max_at_once: 4},
		{name: "mongo-az-b", datasources: ["mongo-dr"], max_at_once: 2},
	]

	celery_broker_datasource:           "redis-broker"
	celery_backend_datasource:          "redis-broker"
	gateway_nonce_datasource:           "redis-broker" // required in production
	watcher_leader_election_datasource: "redis-broker" // one watcher polls at a time

	max_webhook_body_bytes: 5242880 // 5 MiB; a larger webhook is refused with 413

	// The keys the orchestrator reads from global_tags; any other key is read by nothing.
	global_tags: {
		git_cache_dir: "/var/cache/vartrack/git"
		schema_ttl:    "600"
	}
}
