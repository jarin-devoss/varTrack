# Per-Repo Overrides (`vartrack.json`)

Individual repositories can override a subset of their central rule configuration without touching `config.cue`. Place a `vartrack.json` file in the repository and varTrack will apply its settings on top of the matching rule whenever a webhook is received — the third of the [layers a rule is built from](index.md#how-a-rule-is-built), after `config.cue` and the tenant's `bundle.json`.

!!! tip "Check it in CI"
    `vt validate-config` finds the repository's `vartrack.json` and checks it the way a sync reads it; it exits 1 on errors or warnings. See [CI/CD integration](../cli/cicd.md).

---

## Where the file lives

varTrack reads the first of these it finds at the pushed ref:

1. `vartrack.json`
2. `.github/vartrack.json`
3. `.gitea/vartrack.json`
4. `.gitlab/vartrack.json`

The file is read at the pushed commit, so each branch can carry its own — `main` and `develop` can override differently.

---

## How it works

When a push webhook arrives, the ETL pipeline:

1. Resolves the rule: the one in `config.cue`, with the tenant's `bundle.json` rule laid over it (Stage 1 — Payload)
2. Fetches `vartrack.json` from the repository at the pushed commit ref, and resolves the [presets](schema-registry.md#presets-extends) it `extends`
3. Merges the overridable keys from `vartrack.json` into the rule: `branch_map`, `file_path_map`, `variables_map` and `prune` key by key, the others replaced
4. Applies the `config.cue` rule's `force` keys, which win over `vartrack.json`
5. Runs ETL and Sync with the merged settings (Stages 2 and 3)

If `vartrack.json` is absent or empty, the central rule is used unchanged. If it is there but cannot be used — not valid JSON, not a JSON object, or extending a preset that cannot be read — the sync fails with that error: it does not go ahead without settings the repository asked for, such as a `dry_run`. The schema registry is never affected by per-repo overrides.

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

Shared settings can come from presets — here a preset in the org's presets repository, then a key of the repository's own:

```json
{
  "extends": ["local>my-org/presets//repos/checkout"],
  "prune": {"protection": ["experiments.*"]}
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

These keys can be set in `vartrack.json`; what each does, its type, and how it combines with the other layers is in [Rule options](rule-options.md):

<!-- rule-options:repo-overridable -->
- [`branch_as_env`](rule-options.md#branch_as_env)
- [`branch_map`](rule-options.md#branch_map)
- [`dry_run`](rule-options.md#dry_run)
- [`file_name`](rule-options.md#file_name)
- [`file_path_map`](rule-options.md#file_path_map)
- [`pr_as_env`](rule-options.md#pr_as_env)
- [`prune`](rule-options.md#prune)
- [`root_key`](rule-options.md#root_key)
- [`strict_validation`](rule-options.md#strict_validation)
- [`sync_mode`](rule-options.md#sync_mode)
- [`tag_as_env`](rule-options.md#tag_as_env)
- [`variables_map`](rule-options.md#variables_map)
<!-- /rule-options:repo-overridable -->

`branch_map`, `file_path_map`, `variables_map` and `prune` are merged with the rule's key by key — a branch set here is added, and `prune.protection` patterns are added to the rule's, never replace them. The others replace the rule's value.

---

## Security model

Infrastructure and security-critical keys are **always** taken from the central bundle and cannot be changed by the repository:

- `platform`, `datasource`, `repo_url`, `tenant_id`
- `token`, `secret_manager`, `secret_managers`
- `repositories`, `exclude_repositories`
- `self_heal` and all Celery wiring keys

Any key in `vartrack.json` that is not in the overridable list above is silently ignored and logged at `INFO` level.

A key the `config.cue` rule lists under `force` cannot be overridden either: `force` is applied after `vartrack.json`. For example, `force: {prune: {enabled: false}}` keeps a production rule from pruning whatever a repository asks for.

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

**Preset not found** — A preset in `extends` that cannot be read fails the sync with:
```
vartrack.json in https://...: preset local>my-org/presets:nope: no nope.json at main in my-org/presets
```

**Invalid JSON** — A parse error fails the sync with:
```
vartrack.json in https://... is not valid JSON: Expecting ',' delimiter: line 3 column 5 (char 41)
```
Fix the file and push again; the sync runs with it.
