# Onboard a config repository

You have a repository with configuration files — say `configs/app.yaml` — and want every push
to `main` to land in a datasource as `production`, and every push to `develop` as `staging`.
This guide takes it from nothing to the first sync, then adds a check to the repository's CI.

| Step | Who | Where |
|---|---|---|
| [1. Cover the repository with a rule](#1-cover-the-repository-with-a-rule) | the team running varTrack | `config.cue` |
| [2. Register the webhook](#2-register-the-webhook) | the repository's owners | the Git platform |
| [3. Lay out the config file](#3-lay-out-the-config-file) | the repository's owners | the repository |
| [4. Adjust the rule for this repository](#4-adjust-the-rule-for-this-repository-optional) (optional) | the repository's owners | `vartrack.json` |
| [5. Check it in CI](#5-check-it-in-ci) | the repository's owners | the repository's CI |
| [6. Push and watch the sync](#6-push-and-watch-the-sync) | the repository's owners | the web UI or `vt` |

---

## 1. Cover the repository with a rule

A push is synced only when a rule in `config.cue` lists its repository. Ask the team running
varTrack for a rule — or for the repository to be added to an existing one:

```cue
rules: [{
  platform:     "github"
  datasource:   "mongo"
  repositories: ["acme/payments-config"]     // or a pattern: "acme/*-config"
  file_name:    "configs/app.yaml"
  branch_map:   {main: "production", develop: "staging"}
}]
```

A rule is named by its platform and datasource; one rule can cover many repositories with
patterns, and `exclude_repositories` leaves some out. The gateway reads `config.cue` when it
starts, so the change takes effect after the gateway restarts.

!!! tip "Which datasource?"
    The rule's `datasource` is an entry of `datasources` in `config.cue`. The
    [sink pages](../sinks/index.md) say what each one stores and how; for MongoDB the default
    is one nested document per file.

## 2. Register the webhook

In the repository's settings, add a webhook:

| Field | Value |
|---|---|
| Payload URL | `https://<gateway>/webhooks/<datasource>` — e.g. `https://vartrack.example.com/webhooks/mongo` |
| Content type | `application/json` |
| Secret | the platform's `secret` from `config.cue` (ask the team running varTrack) |
| Events | **Pushes**, and **Pull requests** if the rule syncs PRs (`pr_as_env`) |

On GitHub: *Settings → Webhooks → Add webhook*. On Gitea: *Settings → Webhooks → Add webhook →
Gitea*. The gateway checks the signature against the platform's secret; a delivery it cannot
verify is refused with `401`.

A platform-wide (organisation or system) webhook works the same way and saves adding one per
repository.

## 3. Lay out the config file

The rule reads the file at `file_name` (or, per environment, the paths in `file_path_map`) at
the commit that was pushed. Any of the [supported formats](../configuration/formats.md) works.

By default only the `vartrack` section of the file is synced, when there is one:

```yaml
# configs/app.yaml
vartrack:
  database:
    host: prod-db.internal
    port: 5432
  feature_flags:
    new_checkout: true
other_tool:            # not synced: outside the vartrack section
  setting: x
```

`root_key: ""` syncs the whole file, `root_key: "properties"` only that section. Values can be
read from Vault instead of being written in Git — see [Schema annotations](../configuration/annotations.md).

## 4. Adjust the rule for this repository (optional)

Settings that only this repository needs go in a `vartrack.json`, at the repository root or
under `.github/`, `.gitea/` or `.gitlab/`:

```json
{
  "branch_map": {"release": "production"},
  "root_key":   "app",
  "prune":      true
}
```

`branch_map` adds `release` to the rule's branches; `root_key` and `prune` replace the rule's
values. A repository can set only some options — which ones is in
[Rule options](../configuration/rule-options.md) — and `config.cue`'s `force` block still wins.
The file is read at each pushed commit, so `main` and `develop` can carry different ones.
See [Per-repo overrides](../configuration/per-repo-overrides.md).

## 5. Check it in CI

`vt` checks what a sync would do, without writing anything:

```bash
vt validate --file configs/app.yaml --datasource mongo   # the file against its CUE schema
vt validate-config                                       # vartrack.json, the way a sync reads it
```

`vt validate-config` exits 1 when the file has errors (a sync would reject it) or warnings (a
sync would ignore part of it); `--strict` also fails on options under an old name. A
complete GitHub Actions workflow — validate on pull requests, sync on merge — is in
[CI/CD integration](../cli/cicd.md).

`vt` is authorized by the caller's Git token: validating needs read access to the
repository, syncing needs push access. See [Authentication](../configuration/auth.md).

## 6. Push and watch the sync

Push a change to `develop`. Then:

- **Web UI** — the rule's page shows the task, each file's result and the `staging`
  environment; **Events** shows `SyncSucceeded` or why the sync failed.
- **CLI** — `vt task list`, then `vt task get <task-id>`.

Nothing happened? A repository no rule covers, a gateway not restarted after the rule
changed, or a webhook whose secret does not match are the usual causes — see
[Troubleshooting](../operations/troubleshooting.md#a-push-was-not-synced).

!!! warning "Unmapped branches"
    A push to a branch that is not in `branch_map` (and that `branch_as_env`, `pr_as_env`
    or `tag_as_env` does not place) is synced to the environment `default`. Map every branch
    you push to, or give `default` a destination you expect.
