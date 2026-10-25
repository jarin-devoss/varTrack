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

With a `202` and no task, or a task that synced nothing:

- **No rule covers the repository.** The rule's `repositories` patterns must match
  `owner/name`, and `exclude_repositories` must not. The gateway reads `config.cue` when it
  starts — restart it after changing rules.
- **The rule is suspended.** The worker logs `process_webhook task=…: <datasource> is suspended — webhook skipped`.
- **The pushed files are not the rule's.** A rule syncs `file_name`, the `file_path_map` paths,
  or — with neither — the files the push changed. `file_name=… not found in …` means the file
  is not at that path in the pushed commit.
- **The `vartrack` section is missing.** With the default `root_key`, a file that has a
  `vartrack` key syncs only that section; `root_key=… not found in …, skipping` means the
  `root_key` set is not in the file.

!!! warning "A branch that is not mapped still syncs"
    A push to a branch that is not in `branch_map`, and that no `file_path_map`,
    `branch_as_env`, `pr_as_env` or `tag_as_env` places, is synced to the environment
    **`default`**. See [Environment resolution](../concepts/environment-resolution.md).

---

## A sync failed

The task's error, and the `SyncFailed` or `ValidationFailed` event, say why:

| Error | Cause | Fix |
|---|---|---|
| `vartrack.json in … is not valid JSON` / `… must be a JSON object` | The repository's `vartrack.json` cannot be used; the sync does not go ahead without the settings it asked for | Fix the file; `vt validate-config` finds it |
| `… preset local>…: no ….json at main in …` | A preset in `extends` cannot be read | Check the preset's name, branch (`#v2`) and the token's access |
| a schema error, with `strict_validation` | The file does not match its CUE schema | Fix the file, or the schema; without `strict_validation` the failure is only logged |
| `another sync of <datasource>/<env> is still running after 300s` | Two syncs of the same environment; the second waited five minutes for the first | Usually a sync stuck on a slow datasource; the lock is a lease of 600 s, so it frees itself |
| `git_extractor: clone/fetch failed repo=…` | The orchestrator cannot reach the repository or its token is refused | Check the platform's `token` and that the orchestrator can reach the Git server |
| `rolled back: <file> failed — …` | One file failed, and the files written before it in the same sync were restored | Fix the failing file and push again |

A tenant whose `bundle.json` cannot be used has **no rules** until it is fixed; the
orchestrator logs `bundle.json cannot be used — the tenant has no rules until it is fixed: …`.

---

## A setting had no effect

| Log line | Meaning |
|---|---|
| `… sets mongo_uri, self_heal, which only config.cue sets — ignored` | A `bundle.json` set an option only `config.cue` sets |
| `repo_overrides: ignoring non-overridable keys [...]` | A `vartrack.json` set an option a repository may not set |
| `bundle.json of tenant … : no rule in config.cue for <platform>/<datasource> — ignored` | A `bundle.json` rule names a rule `config.cue` does not have |
| `<file>: <old> is now <new>; the old name still works` | An option under its former name (`env_as_branch` → `branch_as_env`) |
| `<file>: <option> was removed and has no effect` | A removed option (`prune.last`, `retry_backoff`, `watcher_state_datasource`) |

Also check `force`: what `config.cue`'s `force` block sets wins over every other layer.
[Rule options](../configuration/rule-options.md) lists which file may set which option, and
`vt validate-config` reports all of the above before a push.

---

## Keys were not pruned

| Cause | How to tell |
|---|---|
| Prune is off, or `force` turns it off | The rule's `prune`, after all layers |
| The sink does not delete single keys | Prune applies to Redis, ZooKeeper, Vercel and MongoDB with `STRATEGY_KEY_VALUE`; whole-object sinks replace the object instead |
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
| `403` | The token's account may not do this: syncing needs push access to the rule's repository, validating needs read access |
| `datasource … has rules on github, gitea; say which with --platform` | Rules on more than one platform write it: pass `--platform` |
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
