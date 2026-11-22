// Example 11: destination_template — one rule, a destination per environment.
//
// The template is the rule's, and has two placeholders: {env} (the environment
// the push resolved to) and {tenant} (the tenant the rule belongs to, "default"
// when no tenants are declared). What it names depends on the sink:
//
//   rule           template            push to main     push to develop   PR #42
//   mongo          "{env}-config"      production-config staging-config   pr-42-config     (collection)
//   zookeeper      "/{tenant}/{env}"   /default/production …              /default/pr-42   (znode root)
//   redis-config   "{tenant}:{env}"    default:production …               default:pr-42    (hash / key prefix)
//
// {env} needs the rule to name its environments: a template with {env} on a rule
// that names none is refused when it loads. Without a template each sink uses its
// own setting (a Mongo collection, a Redis key_prefix, a ZooKeeper base_path).
bundle: {
	platforms: [
		{
			github: {
				endpoint:        "https://github.com"
				push_event_name: "push"
				pr_event_name:   "pull_request"
				secret:          "my-webhook-secret"
				token:           "ghp_xxxxxxxxxxxxxxxxxxxx"
			}
		},
	]

	datasources: [
		{
			mongo: {
				endpoint:        "mongodb://mongo:27017"
				database:        "vartrack"
				collection:      "variables" // used only by a rule with no template
				update_strategy: "STRATEGY_DOCUMENT"
			}
		},
		{
			zookeeper: {
				hosts: ["zk1:2181", "zk2:2181"]
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
			datasource:   "mongo"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			pr_as_env:            true // a pull request is its own environment: pr-<number>
			destination_template: "{env}-config"
		},
		{
			platform:     "github"
			datasource:   "zookeeper"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			pr_as_env:            true
			destination_template: "/{tenant}/{env}"
		},
		{
			platform:     "github"
			datasource:   "redis-config"
			repositories: ["acme-corp/configs"]
			file_name:    "configs/app.yaml"
			branch_map: {main: "production", develop: "staging"}
			pr_as_env:            true
			destination_template: "{tenant}:{env}"
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
