// Example: GitLab as the Git platform instead of GitHub.
bundle: {
	platforms: [
		{
			// Use the "github" type — VarTrack uses the GitHub webhook protocol,
			// which GitLab can also emit (set event_type_header accordingly).
			github: {
				endpoint:          "https://gitlab.my-company.com"
				protocol:          "https"
				token:             "glpat-xxxxxxxxxxxxxxxxxxxx"
				org_name:          "my-group"
				verify_ssl:        true
				timeout:           "30s"
				max_retries:       3

				// GitLab uses X-Gitlab-Event instead of X-GitHub-Event.
				event_type_header: "X-Gitlab-Event"
				push_event_name:   "Push Hook"
				pr_event_name:     "Merge Request Hook"
			}
		},
	]

	datasources: [
		{
			mongo: {
				tag:             ""
				endpoint:        "mongodb://mongo-host:27017"
				database:        "vartrack"
				collection:      "app_vars"
				update_strategy: "STRATEGY_KEY_VALUE"
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

	rules: [
		{
			platform:   "github"
			datasource: "mongo"
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
