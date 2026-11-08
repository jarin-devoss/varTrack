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
- [ ] **`INTERNAL_AUTH_SECRET` on the gateway, the orchestrator and the watcher** (the same
      value, 32+ characters, preferably from `*_FILE`). Without it the orchestrator accepts
      calls from anyone who can reach its HTTP API, gRPC port or admin port, which walks
      around every check the gateway makes. With `APP_ENV=production` all three refuse to
      start without it. See [Internal authentication](internal-auth.md).
- [ ] **The web UI can reach the permission engine** if sign-in is on: `AUTHZ_ADDR` on the
      gateway (TLS from `GATEWAY_TLS_CERT`/`KEY`, signed grants from `INTERNAL_AUTH_SECRET`),
      `GATEWAY_AUTHZ_ADDR` on the orchestrator, and port `5658` open to the orchestrator
      alone. Without it the UI answers `503` to everyone: it never decides for itself. The
      admins are `ui.auth.admins` in the bundle; `UI_AUTH_ADMINS` is no longer read. See
      [the permission engine](internal-auth.md#the-permission-engine).
- [ ] **Admin ports stay internal.** They serve health, metrics, and on the gateway
      `/debug/pprof/` and `POST /admin/opa/reload`. The orchestrator's admin listener binds
      to loopback unless `ADMIN_HOST` says otherwise, and with `INTERNAL_AUTH_SECRET` set
      needs an admin grant for everything but the health probes.
- [ ] **`CORS_ORIGINS`** on the orchestrator lists the web UI's origin; empty allows none, and
      a `*` is ignored in production.

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
      `force: {prune: {enabled: false}}` or `{strict_validation: true}`.
- [ ] **Map every branch you push to.** An unmapped branch is synced to the environment
      `default` ([Environment resolution](../concepts/environment-resolution.md)).
- [ ] **Prune with a dry run first.** A dry run (the UI's *Dry run*, `vt sync --dry-run`) reports what
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
- [ ] **`gateway_nonce_datasource`**: production requires it (the gateway does not start
      without a Redis it can reach), and **`watcher_leader_election_datasource`** when the
      watcher has more than one replica. See [High availability](high-availability.md).
- [ ] **`VARTRACK_ALLOW_DEGRADED` is unset.** Each value is a weaker mode (config on
      environment variables, no `cue` validation, plain TLS, an in-process replay store); the
      last two are refused in production, and none should be needed. Whatever is on shows under
      `degraded` in `/health/readiness`.
- [ ] **Alert on `vartrack_dependency_up == 0`** and on readiness `degraded` being non-empty.

## Checks

- [ ] **CI runs `vt validate-config`** in every config repository and on every tenant's
      `bundle.json`, and `vt validate` on the config files.
- [ ] **Alerts** on refused webhooks, failing tasks, and a stopped watcher —
      see [Monitoring](monitoring.md).
- [ ] **The web UI's sign-in** (`ui.auth`) is set: without it, anyone who reaches the UI sees
      every rule — see [Web UI sign-in](../configuration/auth.md#web-ui-sign-in-with-your-git-platform).
