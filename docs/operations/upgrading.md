# Upgrading

What changes when you move to a new version, and what to do before and after. Read the
section for every version between the one you run and the one you install.

---

## From v0.2.x

This release builds rules in layers, prunes by inventory, reads Git at the pushed commit, and
authorizes the CLI by Git permission. Most configurations keep working; the items under
**Before you upgrade** change what an existing setup does.

### Before you upgrade

| Change | What to do |
|---|---|
| **MongoDB stores one document per file by default.** A Mongo datasource with no `update_strategy` used to store one document per key; it now stores one nested document per file (`STRATEGY_DOCUMENT`). On its next sync each file's per-key documents are replaced, with a **Shape changed** event | To keep per-key documents, set `update_strategy: "STRATEGY_KEY_VALUE"` on the datasource **before** upgrading |
| **The CLI API is authorized by the caller's Git token.** JWT and the RBAC block are gone: `vt` sends a Git personal access token; syncing needs push access to the rule's repository, validating and reading tasks need read access. An `auth.opa` policy, when set, decides with the whole request | Give CI a Git token (`VARTRACK_TOKEN`) instead of a varTrack JWT; see [Authentication](../configuration/auth.md) |
| **One rule per platform and datasource.** A `config.cue` with two rules for the same pair is refused at startup | Give the second destination its own tagged datasource and rule |
| **`host_key_verification: "accept-new"` needs `known_hosts_file`.** Without one it recorded nothing and verified nothing across connections | Set `known_hosts_file` to a writable path, or use `strict` |
| **`vt validate-config` fails on warnings.** It used to exit 0 when a file had only warnings | If a pipeline relied on that, fix the warnings — they are settings a sync ignores |
| **The orchestrator image builds from the repository root.** It generates its own Python stubs | `docker build -f orchestrator-service/docker/Dockerfile .` — the old context (`./orchestrator-service`) no longer builds |

### Options renamed or removed

A `config.cue`, `bundle.json` or `vartrack.json` written for v0.2.x still loads; each of these
is logged, and reported by `vt validate-config` (as a migration for a rename, a warning for a
removal):

| Option | Now |
|---|---|
| `env_as_branch`, `env_as_pr`, `env_as_tags` | `branch_as_env`, `pr_as_env`, `tag_as_env` — the old names still work |
| `prune_protection` | `prune.protection` — still read |
| `prune_last`, `prune.last` | removed: prune always runs after all of an environment's files |
| `retry_backoff` (rule) | removed: nothing read it |
| `watcher_state_datasource` | removed: the watcher keeps no state between polls |
| a rule-level `secrets` block | removed: a rule's `secret_manager` names the entry of `secret_managers` |

### What behaves differently

- **Rules are built in layers**: `config.cue`, then the tenant's `bundle.json`, then the
  repository's `vartrack.json`, then `force`. A `bundle.json` holds only what it changes;
  options only `config.cue` sets are dropped from it with a warning. See
  [Configuration files](../configuration/index.md).
- **Files are read at the pushed commit**, from a bare mirror per repository under
  `GIT_CACHE_DIR`; a push that lands while a sync runs no longer changes what that sync
  reads. Give the API and the workers the same `GIT_CACHE_DIR` volume. The temporary
  `vt_gitcache_*` directories older versions left are removed at startup.
- **Prune is an inventory diff**, once per environment: it deletes the keys varTrack wrote
  before that no file of the environment holds any more, keeps a key something else changed
  (`PruneSkipped`), and never deletes a key another target holds in the same key space
  (`SharedKeySpace`). Targets synced before the upgrade are matched by their rules until
  they are synced again.
- **The watcher asks the orchestrator** which rules to watch and whether each still matches
  Git; it no longer holds sink credentials. It reads `config.cue` only for
  `watcher_leader_election_datasource`.
- **The web UI covers every tenant** a user's Git account can read; the tenant field in
  Settings is gone. Old rule links keep working.
- **The gateway logs event types as `push`, `pull_request` or `other`**, never the header's
  value, and no longer logs delivery IDs or nonces.

### After you upgrade

1. Run `vt validate-config` in each repository that has a `vartrack.json`, and on each
   tenant's `bundle.json` (`vt validate-config bundle.json --tenant <id>`); fix what it
   reports.
2. Watch **Events** for `Shape changed` (MongoDB) and `SharedKeySpace` on the first syncs.
3. Check the gateway's log at startup: it refuses a `config.cue` field it does not know,
   which names the field.
