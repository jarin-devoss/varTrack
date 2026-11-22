// Example 10: Kubernetes sinks: a ConfigMap and a Helm release.
//
// Both run inside the cluster with the orchestrator's service account (no
// kubeconfig_path), or with the kubeconfig the path names. The orchestrator image
// leaves their tools out by default: build it with
//   --build-arg KUBERNETES_CLIENT=kubernetes==31.0.0   (ConfigMap)
//   --build-arg HELM_VERSION=v3.16.3                    (Helm)
//
// A push to main writes:
//   ConfigMap myapp-production, one data entry per key (database.host: "prod-db.internal")
//   Helm release myapp-production: helm upgrade --install … --set database.host=…
//
// Webhooks: https://<gateway>/webhooks/configmap and https://<gateway>/webhooks/helm
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
			configmap: {
				namespace:      "myapp"
				configmap_name: "myapp" // required; a rule's destination_template names it instead
				// kubeconfig_path: "/etc/vartrack/kubeconfig"   // outside the cluster
			}
		},
		{
			helm: {
				namespace:        "myapp"
				release_name:     "myapp" // required; a rule's destination_template names it instead
				chart_name:       "myapp"
				chart_repo:       "https://charts.acme.example"
				chart_version:    "1.4.2"
				upgrade_strategy: "INSTALL_OR_UPGRADE" // or FULL_UPGRADE, VALUES_ONLY
				wait:             true
				wait_timeout:     "5m"
				atomic:           true // roll back a failed upgrade
				max_history:      10
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
			platform:             "github"
			datasource:           "configmap"
			repositories:         ["acme-corp/myapp"]
			file_name:            "deploy/app-settings.yaml"
			branch_map:           {main: "production", develop: "staging"}
			destination_template: "myapp-{env}" // the ConfigMap's name
			self_heal:            true
		},
		{
			platform:             "github"
			datasource:           "helm"
			repositories:         ["acme-corp/myapp"]
			file_name:            "deploy/values.yaml"
			root_key:             "app" // only the app block of the chart values
			branch_map:           {main: "production", develop: "staging"}
			destination_template: "myapp-{env}" // the release's name
			helm_values_mode:     "file"        // a values file instead of --set flags
			self_heal:            true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
