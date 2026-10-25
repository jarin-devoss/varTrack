# Production checklist

What to set before varTrack syncs production configuration. Each item says what goes wrong
without it.

---

## Services

- [ ] **`APP_ENV=production` on the gateway.** Outside production the gateway does **not**
      check webhook signatures or replays, so anyone who can reach it can trigger a sync. Its
      default is `test`.
- [ ] **TLS everywhere.** With `APP_ENV=production` the gateway and the watcher refuse to
      start without it: the gateway needs `GATEWAY_TLS_CERT`/`GATEWAY_TLS_KEY` (inbound) and
      `GRPC_TLS_CERT`/`GRPC_TLS_KEY` (to the orchestrator); the watcher needs `TLS_CA_FILE`,
      `TLS_CERT_FILE` and `TLS_KEY_FILE`. The orchestrator turns mTLS on (`MTLS_ENABLED`), but
      without `TLS_CERT_FILE`/`TLS_KEY_FILE` it **falls back to plain gRPC** with only a log
      line (`MTLS_ENABLED=true but no cert available — gRPC falling back to insecure`): give it
      both, and `TLS_CA_FILE` to verify its clients.
- [ ] **Admin ports stay internal.** They serve health, metrics, and on the gateway
      `/debug/pprof/` and `POST /admin/opa/reload`.
- [ ] **`CORS_ORIGINS`** on the orchestrator lists the web UI's origin; empty allows none.

## Secrets

- [ ] **No credentials written in `config.cue`.** Use `{file: …}` (a mounted secret) or
      `{ref: …}` (Vault) for platform tokens and webhook secrets, datasource passwords and keys,
      the UI's OAuth client secrets — see [Secret value forms](../configuration/bundle.md#secret-value-forms).
      A MongoDB datasource's `password` and a Vault manager's own credentials take only a
      plain string: keep the whole file in a secret mount then.
- [ ] **A webhook `secret` per platform.** Two platforms of the same type are told apart by
      the secret that signed a delivery; when they share one, their webhooks are refused as
      ambiguous.
- [ ] **Tokens with the least access.** A platform's `token` needs read access to the config
      repositories (and the presets); a tenant's `SCHEMA_TENANT_<ID>_TOKEN` read access to its
      schema repository.

## Writing safely

- [ ] **`force` on production rules** for what no tenant or repository may change, e.g.
      `force: {prune: {enabled: false}}` or `{dry_run: false}`.
- [ ] **Map every branch you push to.** An unmapped branch is synced to the environment
      `default` ([Environment resolution](../concepts/environment-resolution.md)).
- [ ] **Prune with a dry run first.** `prune: {enabled: true, dry_run: true}` reports what
      would be deleted; add `protection` patterns for keys other tools own.
- [ ] **MongoDB shape decided.** Set `update_strategy` explicitly on each Mongo datasource;
      changing it later replaces the stored documents.
- [ ] **SSH host keys checked.** `host_key_verification: "strict"` with a `known_hosts_file`
      you filled, or `accept-new` with a writable one; never `off`.
- [ ] **`strict_validation`** where a file that fails its schema must not be written.

## Scaling and state

- [ ] **Redis persistent** (AOF): it holds the sync ledger prune and drift work from, the
      locks, and the UI's history.
- [ ] **Shared `GIT_CACHE_DIR` and `SCHEMA_CACHE_DIR`** for the API and the workers of a host.
- [ ] **`gateway_nonce_datasource`** when the gateway has more than one replica, and
      **`watcher_leader_election_datasource`** when the watcher has.
      See [High availability](high-availability.md).

## Checks

- [ ] **CI runs `vt validate-config`** in every config repository and on every tenant's
      `bundle.json`, and `vt validate` on the config files.
- [ ] **Alerts** on refused webhooks, failing tasks, and a stopped watcher —
      see [Monitoring](monitoring.md).
- [ ] **The web UI's sign-in** (`ui.auth`) is set: without it, anyone who reaches the UI sees
      every rule — see [Web UI sign-in](../configuration/auth.md#web-ui-sign-in-with-your-git-platform).
