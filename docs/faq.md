# FAQ

## Which file do I change?

| To change | Edit | Owned by |
|---|---|---|
| Which repositories are synced, where data goes, credentials, `self_heal`, what may never change (`force`) | `config.cue` | the team running varTrack |
| A tenant's branches, root keys, prune and sync settings for the rules it uses | the tenant's `bundle.json` | the tenant |
| One repository's settings | its `vartrack.json` | the repository's owners |

[Configuration files](configuration/index.md) has the full picture, and
[Rule options](configuration/rule-options.md) which file may set each option.

## I changed `config.cue` — do I need to restart?

The orchestrator re-reads it when the file changes. The gateway reads it once at startup:
restart it after changing platforms, rules or `repositories`. The watcher reads it only for
leader election.

## I changed `bundle.json` — when does it apply?

At the next refresh of the tenant's schema repository: every `SCHEMA_TTL_SECONDS` (300 s), or
at once on a push when the repository is the one `config.cue`'s `schema_registry` names.

## Does `vartrack.json` on a feature branch apply?

Yes, to pushes of that branch: the file is read at the pushed commit, so each branch can
carry its own.

## What happens on a push to a branch I did not map?

It is synced to the environment `default`. Map every branch you push to — see
[Environment resolution](concepts/environment-resolution.md).

## Can a repository turn on `self_heal`, or change where data is written?

No. `self_heal`, `repositories`, `secret_manager`, `force`, `tenants` and the datasources'
connection settings come from `config.cue` only. A repository can set the options listed under
[Overridable keys](configuration/per-repo-overrides.md#overridable-keys), and `force` still wins.

## Will varTrack delete keys I added by hand?

No. Prune deletes only keys varTrack wrote before and Git no longer holds, and keeps a key
whose value something else changed since (with a `PruneSkipped` event). Keys matching a
`protection` pattern are never deleted. See [What prune deletes](concepts/sync-strategies.md#what-prune-deletes).

## Two datasources write the same Redis — will one prune the other's keys?

No. Each sync records the key space it wrote to; a prune keeps every key another target holds
in the same key space, and warns once with a `SharedKeySpace` event.

## How do I try a change without writing anything?

`vt sync --dry-run …` (or `dry_run: true` in the rule) reads, validates and reports what would
be written. `prune: {enabled: true, dry_run: true}` reports what prune would delete.

## Where are secrets kept?

Not in Git. `config.cue` credentials can be `{file: …}` (a mounted secret) or `{ref: …}`
(Vault); a config file's values can be `@secret()` fields its CUE schema marks, fetched from
Vault when the file is synced. varTrack records only digests of secret values. See
[Secret value forms](configuration/bundle.md#secret-value-forms) and
[Schema annotations](configuration/annotations.md).

## Why does the web UI not show a rule?

The UI shows what your Git account can read. A rule whose repositories you cannot read, or
whose platform you have not connected, is counted as hidden; **Connect** signs in to that
platform. Without `ui.auth`, the UI shows every rule.

## Can I run several gateways, workers and watchers?

Yes. Workers and gateways scale out; one watcher leads at a time. Set
`gateway_nonce_datasource` and `watcher_leader_election_datasource` — see
[High availability](operations/high-availability.md).
