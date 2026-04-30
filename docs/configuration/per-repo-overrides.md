# Per-Repo Overrides (`vartrack.json`)

Individual repositories can override a subset of their central rule configuration without touching `config.cue`. Place a `vartrack.json` file at the root of the repository and varTrack will apply its settings on top of the matching rule whenever a webhook is received.

---

## How it works

When a push webhook arrives, the ETL pipeline:

1. Resolves the central rule from `config.cue` (Stage 1 — Payload)
2. Fetches `vartrack.json` from the repository at the pushed commit ref
3. Merges the overridable keys from `vartrack.json` into the active rule config
4. Runs ETL and Sync with the merged settings (Stages 2 and 3)

If `vartrack.json` is absent, malformed, or empty, the central rule is used unchanged. The schema registry is never affected by per-repo overrides.

---

## Example

```json
{
  "root_key": "app_config",
  "branch_map": {
    "main": "production",
    "staging": "staging",
    "develop": "develop"
  },
  "prune": true,
  "sync_mode": "GIT_UPSERT_ALL"
}
```

Place this file at the root of the repository (next to your config file):

```
my-repo/
├── vartrack.json        ← per-repo overrides
└── configs/
    └── app.yaml         ← your config file (watched by the central rule)
```

---

## Overridable keys

These keys can be set in `vartrack.json` to override the central rule:

| Key | Type | Description |
|---|---|---|
| `root_key` | `string` | Subtree to extract before flattening. `""` processes the whole file. |
| `file_name` | `string` | Config file path to watch instead of the centrally configured one. |
| `file_path_map` | `object` | Map of `env → file_path` for per-environment file selection. |
| `branch_map` | `object` | Map of `branch → env` label. Overrides the central branch mapping. |
| `env_as_branch` | `bool` | Use branch name as the environment label. |
| `env_as_pr` | `bool` | Use `pr-{number}` as the environment label. |
| `env_as_tags` | `bool` | Use tag name as the environment label. |
| `sync_mode` | `string` | `AUTO`, `GIT_UPSERT_ALL`, `GIT_SMART_REPAIR`, or `LIVE_STATE`. |
| `prune` | `bool` | Delete keys from the datasource that are no longer present in Git. |
| `strict_validation` | `bool` | Fail the sync if CUE schema validation fails (instead of warn). |
| `variables_map` | `object` | Key-value pairs injected or overridden after ETL transform. |
| `apply_strategy` | `int` | Internal apply strategy (advanced). |
| `dry_run` | `bool` | Permanently shadow this repo — Stage 3 never writes anything. |

---

## Security model

Infrastructure and security-critical keys are **always** taken from the central bundle and cannot be changed by the repository:

- `platform`, `datasource`, `repo_url`, `tenant_id`
- `token`, `secrets`, `secret_managers`
- `repositories`, `exclude_repositories`
- `self_heal` and all Celery wiring keys

Any key in `vartrack.json` that is not in the overridable list above is silently ignored and logged at `INFO` level.

---

## Interaction with the schema registry

`vartrack.json` has **no effect** on CUE schema validation. The schema used to validate your config file is always sourced from the tenant's schema registry (configured in `config.cue` under `schema_registry`). Per-repo overrides cannot change which schema is applied or bypass validation.

---

## Troubleshooting

**Override not applied** — Check the orchestrator logs for:
```
repo_overrides: no vartrack.json repo=... ref=...
```
This means the file was not found at the repo root for that commit ref.

**Keys ignored** — If you see:
```
repo_overrides: ignoring non-overridable keys ['platform', ...] repo=...
```
Those keys are infrastructure-level and cannot be overridden from the repo.

**Invalid JSON** — A parse error produces:
```
repo_overrides: invalid JSON in vartrack.json repo=... ref=...
```
The central rule is used unchanged.
