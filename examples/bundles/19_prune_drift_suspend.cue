// Example 19: keeping a store equal to Git: prune, self-heal, drift_ignore, suspend.
//
// prune        deletes keys varTrack wrote before that Git no longer holds, once per
//              environment after all its files are written. Only on sinks that keep
//              each key (Redis, ZooKeeper, MongoDB STRATEGY_KEY_VALUE). A key someone
//              changed outside varTrack is kept and reported (PruneSkipped); a key
//              matching `protection` is never deleted. Try it with a dry run first:
//                vt sync --file configs/app.yaml --datasource mongo-kv --env staging --dry-run
// self_heal    the watcher asks every POLL_INTERVAL (60 s) whether the store still
//              matches Git, and has the orchestrator write Git's values back.
// drift_ignore keys the application changes itself: shown, never drift, never healed.
// suspend      pushes and self-heal skip the rule; a sync started by hand still runs.
// sync_mode    AUTO (the default) writes every key up to 500 keys a file, and only what
//              differs above; GIT_SMART_REPAIR always writes only what differs.
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
			// One document per key: the MongoDB shape prune can delete single keys from.
			mongo: {
				tag:             "kv"
				endpoint:        "mongodb://mongo:27017"
				database:        "vartrack"
				collection:      "kv_vars"
				update_strategy: "STRATEGY_KEY_VALUE"
			}
		},
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
			datasource:   "mongo-kv"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			sync_mode: "GIT_SMART_REPAIR"
			prune: {
				enabled:    true
				protection: ["_vt_*", "manual.*"]
			}
			self_heal:    true
			drift_ignore: ["feature_flags.rollout_percent", "cache.warmed_at"]
		},
		{
			platform:     "github"
			datasource:   "redis-config"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production"}
			destination_template: "{env}:cfg"
			self_heal:            true
			// Paused during a migration: the web UI shows the rule as suspended.
			suspend: true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
