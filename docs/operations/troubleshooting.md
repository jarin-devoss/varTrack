# Troubleshooting

Start from what you see; each section lists the causes in the order they are most often
found, with the log line or event that confirms each one.

---

## Where to look

| What | Where |
|---|---|
| Did the webhook arrive, and what did the gateway answer? | The platform's webhook page (recent deliveries and their status codes), and the gateway's log |
| What did a sync do? | The web UI: the rule's tasks, each file's result, the **Events** page. Or `vt task list` / `vt task get <id>` |
| Why did a sync fail or skip something? | The orchestrator worker's log. The gateway logs every request with a `correlation_id` and a `request_id`, which identify one delivery in its log |
| Is a datasource drifting? | The web UI's **Environments** page, and the watcher's log |

Logs are JSON lines. `LOG_LEVEL=DEBUG` on a service adds the steps of each decision — for
example which branch rule chose a sync's environment (`env_resolve via=branch_map …`).

---

## A push was not synced

Check the webhook delivery's status code first:

| Status | Meaning | Fix |
|---|---|---|
| `202` | The gateway accepted it and handed it to the orchestrator | Look at the sync's task — see the sections below |
| `200` `event ignored` | The event is neither a push nor a pull request (a `ping`, an issue comment) | Nothing to fix; send **Pushes** (and **Pull requests**) |
| `400` | A missing event header, a body that is not JSON or not a push/PR payload, or a delivery two platforms of one type could both have sent | The response says which. For the last one, give each platform its own `secret` |
| `401` | The signature does not match the platform's `secret`, or the delivery was seen before (replay). Checked only when the gateway runs with `APP_ENV=production` | Set the webhook's secret to the platform's `secret` in `config.cue` |
| `404` | No rule for the datasource in the URL (`/webhooks/<datasource>`) | Use a datasource a rule writes to |
| `413` | The body is larger than `max_webhook_body_bytes` (10 MiB by default) | Raise it in `config.cue` |
| `429` | Rate limit | Retry later; check for a webhook loop |
| `502` / `503` | The orchestrator could not be reached, or the gateway stopped trying for a while (circuit breaker) | Check the orchestrator API's health |
| `503` `GW_REPLAY_PROTECTION_UNAVAILABLE` | The gateway cannot reach the Redis that remembers deliveries, so it cannot tell a replay from a new webhook and refuses it. The platform retries | Restore Redis; readiness shows `nonce_store unavailable` meanwhile |

With a `202` and no task, or a task that synced nothing:

- **No rule covers the repository.** The rule's `repositories` patterns must match
  `owner/name`, and `exclude_repositories` must not. The gateway reads `config.cue` when it
  starts — restart it after changing rules.
- **The rule is suspended.** The worker logs `process_webhook task=…: <datasource> is suspended — webhook skipped`.
- **The pushed files are not the rule's.** A rule syncs only the file it names: `file_name`, or
  the `file_path_map` paths. A push that touched other files does not sync them.
  `file_name=… not found in …` means the file is not at that path in the pushed commit.
- **The `vartrack` section is missing.** With the default `root_key`, a file that has a
  `vartrack` key syncs only that section; `root_key=… not found in …, skipping` means the
  `root_key` set is not in the file.

!!! note "A branch that is not mapped is skipped"
    A push to a branch that is not in `branch_map`, and that no `file_path_map`,
    `branch_as_env`, `pr_as_env` or `tag_as_env` places, feeds no environment of the rule:
    the task ends *nothing to do: branch … feeds no environment of …* and nothing is written.
    There is no `default` environment. See [Environment resolution](../concepts/environment-resolution.md).

---

## A sync failed

The task's error, and the `SyncFailed` or `ValidationFailed` event, say why:

| Error | Cause | Fix |
|---|---|---|
| `vartrack.json in … is not valid JSON` / `… must be a JSON object` / `vartrack.json in …: sync_mode "…" is not a mode: use AUTO, GIT_UPSERT_ALL or GIT_SMART_REPAIR` | The repository's `vartrack.json` cannot be used; the sync does not go ahead without the settings it asked for | Fix the file; `vt validate-config` finds it |
| `… preset local>…: no ….json at main in …` | A preset in `extends` cannot be read | Check the preset's name, branch (`#v2`) and the token's access |
| a schema error, with `strict_validation` | The file does not match its CUE schema | Fix the file, or the schema; without `strict_validation` the failure is only logged |
| `another sync of <datasource>/<env> is still running after 300s` | Two syncs of the same environment; the second waited five minutes for the first | Usually a sync stuck on a slow datasource; the lock is a lease of 600 s, so it frees itself |
| `git unavailable: cannot read <repo> at <ref>: …` | The orchestrator cannot clone or fetch the repository, or its token is refused. The task is `FAILED`, not "no files": nothing was written and nothing was pruned | Check the rule's platform `token` in `config.cue` (a rule reads its own repositories with it, and only from the server that platform names) and that the orchestrator can reach the Git server |
| `failed: 1 file(s) could not be processed (app.yaml: ScannerError: …)` | A pushed file does not parse (or fails `strict_validation`). The other files of the push are still written, nothing is pruned, and the task is `FAILED` | Fix the file named, and push again |
| `failed: 2 of 3 file(s): a.yaml: …; b.yaml: …` | Some files were written and some were not; each file's reason is listed, the written ones show as written | Fix the files named |
| `secrets unavailable: database.password: cannot read vartrack/configs#database.password from secret manager 'default': …` | Vault (or the secret manager) cannot be read. The file is not written at all, rather than written with the placeholder the file holds; the datasource keeps its last value | Restore the secret manager; push again, or let the watcher heal |
| `secrets unavailable: … is a @secret field, but config.cue defines no usable secret manager` | A schema marks a field `@secret` and `config.cue` has no secret manager the rule can use | Add one to `secret_managers`, or name it with the rule's `secret_manager` |
| `schemas unavailable: tenant <t>: the schema repository … could not be cloned: …` | The tenant's schema repository cannot be read (or its token cannot be). Its rules fail with this reason instead of running as a tenant with no schemas; the clone is retried after `SCHEMA_TTL_SECONDS` | Check the tenant's `schema_repo` and its token |
| `the config breaks N rules its protos declare: - datasources[1].redis.database: …` | `config.cue` sets a value a proto's rule refuses, or leaves out something a proto requires. Every service refuses it when it starts, and `vartrack-bundle check` prints the same list | Fix each line it names; the path says which field. [What the loader checks](../configuration/bundle.md#what-the-loader-checks) |
| `config.cue unavailable: …` (`503` / gRPC `UNAVAILABLE`) | The orchestrator cannot read `config.cue`: `vartrack-bundle` fails or is missing. With a bundle it already read, it keeps using that one (readiness: `config.cue: stale`); with none, nothing is served | Fix `config.cue` (`vartrack-bundle export config.cue` prints the error) |
| `cannot validate app.yaml: the 'cue' program is not installed` | The file has a CUE schema and the orchestrator has no `cue` binary. The file fails under `strict_validation`, and is written with a `warn` otherwise | Install `cue`; or accept writing unvalidated files with `VARTRACK_ALLOW_DEGRADED=schema_validation` |
| `the sync lock for <datasource>/<env> cannot be taken: …` | Redis does not answer, so two syncs of one place cannot be kept apart. In production the sync stops; elsewhere it goes on without a lock | Restore Redis |
| `schema <name> (schema_map[N], <glob>) is not in the schema repository of tenant <t> at <commit>` | A `schema_map` entry (or a file's `$schema` with `schema_from_file`) names a schema the tenant's schema repository does not have at the commit the task ran on. The file is not written, `strict_validation` or not. The orchestrator logged the same when it cloned or refreshed the schema repository | Push the schema, or fix the entry; `none` maps a path to "not checked" on purpose |
| `strict_validation: no schema checks <file> (rule <platform>/<datasource>): add a schema_map entry …` | The rule is strict and no schema matches the file by `schema_map`, by name or as `default.cue`. Before, such a file was synced unchecked | Add a `schema_map` entry for it, or map it to `none` |
| A file's `validation` is `none` | Nothing checked it: no schema was found, the path is mapped to `none`, or the `cue` program is not installed. It is never shown as `ok` | Map it to a schema if it should be checked |
| `rolled back: <file> failed — …` | One file failed, and the files written before it in the same sync were restored (only a MongoDB datasource can: it snapshots a file before writing it) | Fix the failing file and push again |
| `not rolled back: <file> stays written in <env> (… keeps no snapshot to restore); <file2> failed — …` | One file failed after earlier files were written to a datasource that cannot take a write back (Redis, ZooKeeper, S3, a file, a ConfigMap, Helm). Those files are reported `partial`: they are in the store, and the ledger counts them as desired state. The failed and unattempted files are not | Fix the failing file and push again; the next sync, or self-heal, writes the desired state over what was left |
| `rollback failed: <file> stays written in <env> (<error>)` | The datasource could restore, tried, and could not (the store went away) | Restore the store and push again |

A tenant whose `bundle.json` cannot be used has **no rules** until it is fixed; the
orchestrator logs `bundle.json cannot be used — the tenant has no rules until it is fixed: …`.

**Which schemas did a sync use?** A finished task reports `schema_commit`: the commit of the tenant's schema
repository its files were validated against (`GET /v1/tasks/<id>`; `""` when the tenant has no schema repository).
After a push to the schema repository, a sync whose `schema_commit` is still the old commit ran on stale schemas:
every process keeps its own clone and refetches when the tenant's schema generation (a counter in Redis, advanced by
a schema push or `POST /v1/tenants/<t>/schemas/warm`) differs from the one it was fetched at. If Redis cannot take the
new generation, the orchestrator logs `schema generation of tenant <t> not published: other processes refresh on their
TTL`, and clones are refetched only after `SCHEMA_TTL_SECONDS`.

---

## Sync is refused in the web UI

| What the UI says | Meaning | Fix |
|---|---|---|
| `nothing feeds <env> in the <datasource> rule: no branch_map entry maps a branch to it, branch_as_env is off, and it is not a file_path_map environment (an earlier version of the rule wrote it; it is not synced any more)` | The environment is in the store (an earlier version of the rule wrote it, for example before `branch_as_env` was turned off) but the rule as it is now produces nothing that lands in it. This is the answer for admins too: there is no branch to read, so nothing could be synced. Sync and Dry run are disabled on it, and its keys and history stay visible | Map a branch to it (`branch_map`), turn `branch_as_env` back on, or leave it: nothing writes it any more |
| `<platform>/<datasource> names no file to read: set file_name (for example "app-settings.yaml") or file_path_map in config.cue or the tenant's bundle.json` | The rule says nothing about which file it reads, so it is **refused** when loaded, like a rule with no environments: nothing runs it, the Rules page lists it as *Refused*, and a push, sync or dry run of it answers with this. A repository's `vartrack.json` can still change the file, but cannot be the only place that names one | Give the rule a `file_name` or a `file_path_map` in `config.cue` or the tenant's `bundle.json` |
| `<platform>/<datasource> names no environments: set branch_map (for example {"main": "production"}), file_path_map, branch_as_env, pr_as_env or tag_as_env in config.cue or the tenant's bundle.json` | The rule says nothing about where its environments come from, so it is **refused** when loaded: nothing runs it, the Rules page lists it as *Refused*, and a push, sync or dry run of it answers with this | Name them in `config.cue` or the tenant's `bundle.json`. To keep writing to an environment called `default`, set `branch_map: {"main": "default"}` |
| `<platform>/<datasource>: sync_mode "<value>" is not a mode: use AUTO, GIT_UPSERT_ALL or GIT_SMART_REPAIR` | The tenant's `bundle.json` gives the rule a mode varTrack does not have (a typo is not read as `AUTO`), so it is **refused** when loaded: nothing runs it, the Rules page lists it as *Refused*, and a push, sync or dry run of it answers with this | Set one of the three, or delete the line (`AUTO`) |
| `nothing to do: branch <branch> feeds no environment of <rule>` | A push to a branch the rule's `branch_map` (and `branch_as_env`, `pr_as_env`, `tag_as_env`) does not place. It is skipped; nothing is written to an environment nobody declared | Map the branch, or ignore the push |
| `<login> on <platform> may not push to <branch> of <repo> (the branch that feeds <env>)` | A Sync is judged as a direct push to the branch that feeds the environment, so branch protection counts. A rule's environments come only from the strategies it names, so there is no fallback branch | Ask for push rights, or use Dry run |

A page shows such an environment as *No longer synced by this rule*. Every environment has its own **Diff**, but this one's is disabled ("no branch feeds it, so there is nothing in Git to compare with"), its keys are not compared with Git (`GET …/targets/{env}/keys` answers `409`), and its panel shows only *Summary* and *History*; what varTrack last wrote there is labelled **Last written by varTrack**, not *Desired*. An environment the rule produces and has never written has a disabled Diff too ("Not synced yet"). It is not compared with the rule (its keys sit under a destination the rule no longer writes, so it would read drifted for ever), it does not count in the rule's status, and the watcher does not heal it. An administrator can drop it from varTrack's record with **Forget** on its box (or `DELETE /v1/ui/rules/{platform}/{datasource}/targets/{env}`); its events and tasks stay, and the keys in the store are left alone.

An environment the rule produces and has never written (a new rule, or a `branch_map` entry just added) is listed on the rule's page as *Not synced yet*, with the branch that feeds it; **Sync** (or **Dry run**) writes it the first time. If Sync and Dry run have nothing to start, the panel says why: no branch feeds any environment of the rule, and a dry run, which writes nothing, still reads the branch that feeds the environment.

Which branch feeds an environment is the same answer everywhere: a `branch_map` entry, else (for a `file_path_map` environment) the rule's own `branch`, else (with `branch_as_env`) the environment's own name; otherwise nothing feeds it. Self-heal reads the same branches.

---

## A setting had no effect

| Log line | Meaning |
|---|---|
| `… sets endpoint, self_heal, which only config.cue sets — ignored` | A `bundle.json` set an option only `config.cue` sets (a datasource field such as `endpoint` belongs in the datasource's block) |
| `… sets mongo_uri, which is not an option a tenant may set — ignored` | A key no rule option and no datasource field declares, such as an old flat connection name |
| `… sets tenant_id, which no file may set — the tenant is the one whose schema repository holds this file (acme); ignored` | A `bundle.json` or `vartrack.json` wrote a `tenant_id`. The tenant is where a file lives, never something it says, so the key never did anything (when it names another tenant the line says so). `vt validate-config` fails the file; delete the key |
| `repo_overrides: ignoring non-overridable keys [...]` | A `vartrack.json` set an option a repository may not set |
| `bundle.json of tenant … : no rule in config.cue for <platform>/<datasource> — ignored` | A `bundle.json` rule names a rule `config.cue` does not have |
| `<file>: <old> is now <new>; the old name still works` | An option under its former name (`env_as_branch` → `branch_as_env`) |
| `<file>: <option> was removed and has no effect` | A removed option (`prune.last`, `retry_backoff`, `watcher_state_datasource`, and `tenant_id` in a `config.cue` rule) |
| `<file>: dry_run was removed and has no effect; a dry run is started, not configured …` | A rule set `dry_run: true`, which made it a "shadow rule" that never wrote. **The rule now writes on every push.** Delete the line, and check a change in CI with `vt sync --dry-run` (or the dry-run webhook) instead |
| `<file>: prune.dry_run was removed …` | A prune block set `dry_run: true`, which reported deletes instead of running them. Delete the line: a dry run reports what a prune would delete, and a rule with `prune` off deletes nothing. A block that had only `dry_run` is now an empty, disabled block |

Also check `force`: what `config.cue`'s `force` block sets wins over every other layer.
[Rule options](../configuration/rule-options.md) lists which file may set which option, and
`vt validate-config` reports all of the above before a push.

---

## Keys were not pruned

| Cause | How to tell |
|---|---|
| Prune is off, or `force` turns it off | The rule's `prune`, after all layers |
| The sink does not delete single keys | Prune applies to Redis, ZooKeeper and MongoDB with `STRATEGY_KEY_VALUE`; whole-object sinks replace the object instead |
| It is the environment's first sync | The first sync prunes nothing: varTrack deletes only keys it wrote before |
| The key was changed outside varTrack | Kept, with a `PruneSkipped` event naming it |
| A `protection` pattern matches it | Patterns of every layer add up |
| Another target holds the key in the same key space | A `SharedKeySpace` warning names the other target |
| A file of the environment failed | Prune runs only when every file of the environment was written |
| The ledger or a sink could not be read | `prune: … cannot be read … - nothing is pruned`; the next sync retries |

See [What prune deletes](../concepts/sync-strategies.md#what-prune-deletes).

---

## MongoDB documents were replaced

A **Shape changed** event, and the orchestrator's `MongoSink: update_strategy … replaced N
documents of the previous shape` warning, mean the datasource's `update_strategy` changed since
the file was last written: the documents of the other shape were removed. A datasource with no
`update_strategy` stores one nested document per file (`STRATEGY_DOCUMENT`); set
`update_strategy: "STRATEGY_KEY_VALUE"` to keep one document per key. See [MongoDB](../sinks/mongodb.md).

---

## Drift is not healed

- `self_heal` is set only in `config.cue`; a `bundle.json` value is dropped.
- The rule is `suspend`ed, or the drifting keys match `drift_ignore` (shown as `ignored`).
- Several watcher replicas without `watcher_leader_election_datasource`: set it so one leads.
  Replicas that are not the leader stay idle by design.
- `HealFailed` events: the orchestrator could not write; the watcher backs off while heals
  keep failing.

See [Drift detection](../concepts/drift-detection.md).

---

## The CLI or the web UI refuses

| Message | Fix |
|---|---|
| `401` from `vt` | The Git token is missing or refused: `vt login`, or `VARTRACK_TOKEN` in CI |
| `403` | The token's account may not do this: syncing needs push access to the branch that feeds the environment (a protected branch counts), validating needs read access. The message says which branch and why; a web UI *Sync* is refused for the same reason |
| `datasource … has rules on github, gitea; say which with --platform` | Rules on more than one platform write it: pass `--platform` |
| `say which Git platform the token is for (--platform): …` | The request names no rule and the gateway has several platforms: `vt login --platform`, `VARTRACK_PLATFORM` or `--platform` |
| `503 permissions are unavailable` from the web UI | The orchestrator cannot reach the gateway's permission engine: check `AUTHZ_ADDR` on the gateway and `GATEWAY_AUTHZ_ADDR` on the orchestrator, and that they share `INTERNAL_AUTH_SECRET` ([Internal authentication](internal-auth.md#the-permission-engine)) |
| `limit must be a whole number from 0 to 2147483647` | `vt task list --limit` out of range |
| The UI lists fewer rules than expected, with "hidden" counts | The signed-in account cannot read those repositories, or has not connected that platform — **Connect** signs in to it |

---

## SSH (Linux server sink)

| Error | Fix |
|---|---|
| `host_key_verification accept-new needs known_hosts_file` | Set `known_hosts_file` to a writable path: `accept-new` records new hosts there |
| `known_hosts_file … does not exist` (strict) | Create it with the server's key (`ssh-keyscan host >> file`) |
| `BadHostKeyException` | The server's key changed since it was recorded. Confirm the change, then replace its line in `known_hosts_file` |

See [Linux server](../sinks/linux-server.md#host-keys).
