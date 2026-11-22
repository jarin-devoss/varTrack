# varTrack examples

Bundles, config files, schemas and layer files to copy from. Every bundle here loads as the gateway reads it (CI checks
that, and that each name a bundle uses is a datasource it declares). The full reference is the
[documentation](https://jarin-devoss.github.io/varTrack/); start with [Configuration files](../docs/configuration/index.md).

## Bundles (`bundles/`)

A bundle is `config.cue`: the one file every service reads, at `CONFIG_PATH`. Check one with `vartrack-bundle check config.cue`.

| File | Shows |
|---|---|
| `01_mongodb_basic.cue` | The smallest bundle that runs: GitHub, MongoDB, and the Redis the services need |
| `02_mongodb_auth.cue` | MongoDB with a user, a password from a mounted secret, and TLS |
| `03_zookeeper.cue` | ZooKeeper: one znode per key, `base_path`, prune, the watcher's lock on ZooKeeper |
| `04_redis_sink.cue` | Redis as the sink, in its own database next to the Celery broker; `prune.protection` |
| `05_s3_sink.cue` | S3 or MinIO, with each file stored as a file (`STRATEGY_FILE`) |
| `06_linux_server_sink.cue` | A file on a server over SSH: host keys, file mode, a reload after the write |
| `07_multi_sink.cue` | One file into MongoDB, ZooKeeper and Redis: one rule and one webhook per datasource |
| `08_dual_datasource_same_type.cue` | Two MongoDBs (primary and DR) told apart by `tag` |
| `09_vault_secrets.cue` | Vault: `{ref: …}` credentials, and `@secret()` fields read through the file's schema |
| `10_kubernetes_configmap_helm.cue` | A Kubernetes ConfigMap and a Helm release |
| `11_destination_template.cue` | `{env}` and `{tenant}` in `destination_template`, with pull request environments |
| `12_production_full.cue` | Production: two platforms, no credential in the file, `force`, `workers`, `sync_limits`, the replay store |
| `13_cli_auth_opa.cue` | Who may use `vt`: Git permissions, then an OPA policy |
| `14_gitea_platform.cue` | Gitea as the Git platform |
| `15_web_ui_sign_in.cue` | Web UI sign-in with GitHub and Gitea |
| `16_tenants.cue` | Two tenants on one Redis, each with its schema repository and its own keys |
| `17_schema_validation.cue` | `schema_map`, `schema_from_file`, `strict_validation`, `file_path_map` |
| `18_environments.cue` | `branch_map`, `pr_as_env`, `tag_as_env`, and one file holding every environment |
| `19_prune_drift_suspend.cue` | `prune`, `self_heal`, `drift_ignore`, `suspend`, `sync_mode` |

### Datasource names

A datasource is named by its type, and its `tag` when it has one:

```
mongo  {}                  → "mongo"
mongo  {tag: "primary"}    → "mongo-primary"
redis  {tag: "config"}     → "redis-config"     (tag "redis-config" would be "redis-redis-config")
```

The name is what a rule's `datasource` says, what `celery_broker_datasource`, `gateway_nonce_datasource` and
`watcher_leader_election_datasource` say, and the webhook URL on the gateway: `POST https://<gateway>/webhooks/<name>`.

### What every bundle here sets

- A webhook `secret` on each platform. With `APP_ENV=production` the gateway checks every delivery against it.
- `celery_broker_datasource`, `celery_backend_datasource` and `gateway_nonce_datasource`, at the top of the bundle (not
  in `global_tags`, whose only keys are `git_cache_dir`, `git_cache_max_entries`, `schema_cache_dir` and `schema_ttl`).
  Production does not start without `gateway_nonce_datasource`.
- A file (`file_name` or `file_path_map`) and the environments (`branch_map`, …) on every rule: a rule without them is refused.
- `update_strategy` on each MongoDB, S3 and other file-keeping datasource, on purpose: changing it later replaces what is stored.

## Config files (`configs/`)

Files a config repository holds. The format is the file's extension.

| File | Format | |
|---|---|---|
| `app.yaml` | YAML | passes `schemas/app.yaml.cue`; two fields are `@secret()` |
| `settings.json` | JSON | passes the closed `schemas/settings.json.cue` |
| `vars.env` | `.env` | passes `schemas/vars.env.cue`: every value is text, and `int` and `bool` still check it |
| `annotated.yaml` | YAML | passes `schemas/annotated_schema.cue`; synced by `bundles/09_vault_secrets.cue` |
| `database.toml` | TOML | sections become key prefixes |
| `params.ini` | INI (`.ini`, `.cfg`) | `section.key`; every value is text |
| `config.hcl` | HCL (`.hcl`, `.tf`) | blocks become key prefixes |
| `settings.multi-env.json` | JSON | every environment in one file: `default`, `staging` and `production` under a `vartrack` root, merged key by key. Its `$schema` names a schema only for a rule with `schema_from_file`, and is never synced |
| `settings.multi-env.yaml` | YAML | the same with `root_key: config`, a value per environment inside a list item, and a block that is never synced (`bundles/18_environments.cue`) |

A value written `@secret(mount/path#field)` is replaced with Vault's value before anything is written, **for a field
the file's schema marks `@secret()`**. In a field no schema marks, the text is written as it is.

## Schemas (`schemas/`)

CUE schemas, kept in a tenant's **schema repository** (not in the config repository). A file is checked by the schema
named after it at the repository's root (`app.yaml` → `app.yaml.cue`), unless a rule's `schema_map` names another.

| File | Checks |
|---|---|
| `app.yaml.cue` | `configs/app.yaml`: types, ranges, an enum, optional keys, a pattern for every `feature_flags.*` key |
| `settings.json.cue` | `configs/settings.json`, closed: a key the schema does not declare fails |
| `vars.env.cue` | `configs/vars.env` |
| `annotated_schema.cue` | `configs/annotated.yaml`, named by a `schema_map` entry: `@secret()`, `@secret(ref=…)` and `@logger()` |

The fields are quoted dotted keys at the top level (`"database.port": int & >=1`): the file is flattened before it is
checked, and a schema written inside a definition (`#Config: {…}`) checks nothing. Try one with
`vt validate --file configs/app.yaml --datasource mongo`.

## Layers (`layers/`)

The files that adjust a `config.cue` rule, for the payments tenant of `bundles/16_tenants.cue`:

| File | Lives in | |
|---|---|---|
| `bundle.json` | the tenant's schema repository, at its root | the tenant's layer: adds a branch, turns on strict validation, extends a preset |
| `presets/team.json` | the same repository, `presets/team.json` | a preset `bundle.json` extends (`local>payments/schemas//presets/team`) |
| `vartrack.json` | a config repository (or its `.github/`, `.gitea/`, `.gitlab/`) | the repository's layer: a release branch, a key rename, a protected key |

Check them before you push: `vt validate-config bundle.json --tenant payments`, and `vt validate-config` in the config repository.

## Policies (`policies/`)

| File | |
|---|---|
| `vartrack_authz.rego` | An OPA policy for `auth.opa.policy_file`: admins sync anywhere, writers outside production and never `configs/secrets/*`, dry runs anywhere. `bundles/13_cli_auth_opa.cue` has it inline |

## Webhooks (`webhooks/`)

| File | |
|---|---|
| `curl_examples.sh` | Signed GitHub and Gitea deliveries sent by hand, an ignored event, the health checks |
| `github_webhook_setup.md` | Adding a GitHub webhook, the secret, and reading a delivery's answer |
