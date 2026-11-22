// Example 09: secrets from Vault, kept out of Git.
//
// Two different things read Vault:
//
// 1. A credential in this bundle, written {ref: {path, key}}: the Gitea token and
//    the Mongo password below.
//
// 2. A value in a config file, for a field its CUE SCHEMA marks @secret(). Only a
//    field the schema marks is read from Vault: without a schema, the text
//    "@secret(...)" would be written to the sink as it is. So this bundle names a
//    schema registry, and the rule maps its file to a schema:
//
//    configs/annotated.yaml in Git (examples/configs/annotated.yaml):
//      database:
//        password: "@secret(secret/myapp/db#password)"
//    annotated_schema.cue in the schema repository (examples/schemas/annotated_schema.cue):
//      "database.password": string & !="" @secret() @logger()
//      "auth.jwt_secret":   string & !="" @secret(ref="auth-vault")
//
//    The sink gets Vault's value; a dry run shows it as ***; the sync ledger keeps
//    only a digest. A secret that cannot be read fails the file: the placeholder
//    is never written instead.
//
// Vault, for the file above:
//   vault kv put secret/myapp/db   password=supersecret123
//   vault kv put secret/myapp/api  stripe_key=sk_live_xxx sendgrid_key=SG.xxx
//   VAULT_ADDR=https://vault-auth.internal:8200 \
//   vault kv put secret/myapp/auth jwt_secret=jwt123 refresh_secret=refresh456
bundle: {
	platforms: [
		{
			gitea: {
				endpoint: "https://gitea.internal"
				secret:   {ref: {path: "vartrack/gitea", key: "webhook_secret"}}
				token:    {ref: {path: "vartrack/gitea", key: "token"}}
			}
		},
	]

	datasources: [
		{
			mongo: {
				host:            "mongo.internal"
				username:        "vartrack"
				password:        {ref: {path: "vartrack/mongo", key: "password"}}
				database:        "vartrack"
				collection:      "variables"
				update_strategy: "STRATEGY_DOCUMENT"
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

	secret_managers: [
		{
			// Tagged "default": the one @secret() and {ref: …} use unless told otherwise.
			vault: {
				tag:         "default"
				endpoint:    "https://vault.internal:8200"
				mount_point: "secret"
				kv_version:  2
				// Vault's own login is a string or a {file: …}, never a {ref: …}.
				token_auth: {token: {file: "/run/secrets/vault-token"}}
				// Where an @secret() field the file leaves as a plain value is read,
				// under the field's own name. "@secret(path#field)" needs none.
				// path_prefix: "myapp/prod"
			}
		},
		{
			// @secret(ref="auth-vault") in the schema reads from this one.
			vault: {
				tag:         "auth-vault"
				endpoint:    "https://vault-auth.internal:8200"
				mount_point: "secret"
				kv_version:  2
				app_role_auth: {
					role_id:   "vartrack"
					secret_id: {file: "/run/secrets/vault-auth-secret-id"}
				}
			}
		},
	]

	// The repository the CUE schemas are read from.
	schema_registry: {
		platform: "gitea"
		repo:     "platform/schemas"
		branch:   "main"
	}

	rules: [
		{
			platform:     "gitea"
			datasource:   "mongo"
			repositories: ["acme/configs"]
			file_name:    "configs/annotated.yaml"
			branch_map: {main: "production"}
			// This file is checked by this schema. A schema found by the file's name
			// would be annotated.yaml.cue at the schema repository's root.
			schema_map: [{match: "configs/annotated.yaml", schema: "annotated_schema.cue"}]
			// A file the schema refuses is not written.
			strict_validation: true
			// The rule's {ref: …} credentials and @secret() fields come from here.
			secret_manager: "vault-default"
		},
	]

	celery_broker_datasource:  "redis-broker"
	celery_backend_datasource: "redis-broker"
	gateway_nonce_datasource:  "redis-broker"
}
