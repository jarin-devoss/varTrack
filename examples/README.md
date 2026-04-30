# VarTrack Examples

This folder contains ready-to-use examples for configuring and integrating VarTrack.

## Bundles (`bundles/`)

CUE bundle configs — the live configuration loaded by all VarTrack services.

| File | Description |
|---|---|
| `01_mongodb_basic.cue` | Minimal MongoDB sink with GitHub platform |
| `02_mongodb_auth.cue` | MongoDB with username/password auth, TLS, replica set |
| `03_zookeeper.cue` | ZooKeeper 3-node quorum as config sink |
| `04_redis_sink.cue` | Redis as a config store (separate from Celery broker DB) |
| `05_s3_sink.cue` | S3 / MinIO object store as config sink |
| `06_linux_server_sink.cue` | SSH to a Linux host, write config files remotely |
| `07_multi_sink.cue` | Fan-out: one push writes to MongoDB + ZooKeeper + Redis |
| `08_dual_datasource_same_type.cue` | Primary + DR: two MongoDB sinks differentiated by `tag` |
| `09_vault_secrets.cue` | `@secret()` annotations — Vault resolves secrets at ETL time |
| `10_gitlab_platform.cue` | GitLab (self-hosted) as the Git platform |
| `11_destination_template.cue` | `{env}` interpolation: one rule fans out to per-env destinations |
| `12_production_full.cue` | Full production bundle: multi-platform, multi-sink, Vault |
| `13_auth_oidc_rbac_opa.cue` | OIDC login, RBAC roles, OPA policy for CLI access control |
| `14_gitea_platform.cue` | Gitea (self-hosted) as the Git platform |

### How datasource naming works

The datasource name used in webhook URLs is derived from the `tag` field:

```
tag: ""          → name = type         → "mongo", "zookeeper", "redis"
tag: "primary"   → name = "type-tag"   → "mongo-primary"
tag: "dr"        → name = "type-tag"   → "mongo-dr"
tag: "cfg"       → name = "type-tag"   → "redis-cfg"
```

Webhook URL: `POST /v1/webhooks/{datasource}`

## Config files (`configs/`)

Sample configuration files your Git repo can contain. VarTrack parses these formats automatically based on file extension.

| File | Format | Extension |
|---|---|---|
| `app.yaml` | YAML | `.yaml`, `.yml` |
| `database.toml` | TOML | `.toml` |
| `settings.json` | JSON | `.json` |
| `vars.env` | dotenv | `.env` |
| `config.hcl` | HCL / Terraform | `.hcl`, `.tfvars` |
| `params.ini` | INI | `.ini`, `.cfg` |

`@secret(path#field)` annotations in any format are resolved by Vault before writing to the sink.

## Schemas (`schemas/`)

CUE validation schemas — define the shape and constraints for your config files.

| File | Description |
|---|---|
| `app_schema.cue` | Open struct: validates specific keys, allows extras |
| `strict_schema.cue` | Closed struct: rejects any undeclared key |
| `annotated_schema.cue` | Full example with `@secret()` and `@logger()` on every field type |

Reference a schema in your rule settings to validate every push before it reaches the sink.

## Webhooks (`webhooks/`)

| File | Description |
|---|---|
| `curl_examples.sh` | `curl` one-liners for all sink types + bundle push |
| `github_webhook_setup.md` | Step-by-step GitHub webhook configuration guide |
