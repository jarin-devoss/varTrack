// Example 02: MongoDB with a user, a password from a mounted secret, and TLS.
//
// The sink builds the connection string from host, port, username and password
// only when `endpoint` is left out. With `endpoint` set, that string is used as
// it is, and username and password are not read: a replica set, for example, is
//   endpoint: "mongodb://app-user:<password>@mongo-1:27017,mongo-2:27017/?replicaSet=rs0&authSource=admin"
// (then keep the whole config.cue in a secret mount).
bundle: {
	platforms: [
		{
			github: {
				endpoint: "https://github.com"
				secret:   {file: "/run/secrets/github-webhook-secret"}
				token:    {file: "/run/secrets/github-token"}
			}
		},
	]

	datasources: [
		{
			mongo: {
				host:     "mongo.internal"
				port:     27017
				username: "app-user"
				// Read on the orchestrator's machine; a username needs its password.
				password: {file: "/run/secrets/mongo-password"}

				database:        "vartrack"
				collection:      "variables"
				update_strategy: "STRATEGY_DOCUMENT"

				// TLS: the server's certificate is checked against this CA.
				ssl:         true
				ssl_ca_path: "/etc/ssl/mongo-ca.pem"

				// Fail a write after 5 s when the server is down, instead of the driver's
				// own waits (20 s to connect, 30 s to find a server).
				connect_timeout_ms: 5000
			}
		},
		{
			redis: {
				tag:      "broker"
				hosts:    ["redis:6379"]
				password: {file: "/run/secrets/redis-password"}
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
			branch_map: {main: "production"}
			self_heal: true
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
