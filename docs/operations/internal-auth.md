# Internal authentication

The gateway checks webhook signatures, Git permissions and policy. The orchestrator sits
behind it, but it has listeners of its own: an HTTP API, a gRPC port and an admin port. This
page is about how those are protected, so that the checks the gateway makes cannot be
walked around by calling the orchestrator directly.

The rule is simple: **the orchestrator does not trust where a call comes from, only what it
carries.** Every call to it carries a short-lived signed *grant*. The gateway mints the
grants for webhooks and CLI calls after it has made its own checks; the watcher mints its
own; an operator mints one for the metrics scrape.

---

## What is protected

| Listener | Needs | Open without it |
|---|---|---|
| HTTP API `:8000` — webhooks, dry runs, schema warm-up, API docs | a `sync` grant | `/v1/health*`, `/v1/auth/*` (the sign-in flow) |
| HTTP API `/v1/ui/*` | the web UI's signed-in session | — |
| HTTP API `GET /v1/tasks/{id}`, `GET /v1/tenants/{id}/schemas` | a `sync` grant **or** a signed-in session | — |
| gRPC `:50051` — `Orchestrator`, `CLIService` | a `sync` grant on every call | — |
| gRPC `:50051` — `Watcher` | a `watch` grant on every call | — |
| Admin `:9093` — `/metrics`, `/circuit-breaker`, `/debug/pprof` | an `admin` grant | `/healthz`, `/health/*` |

A route added later is closed until someone opens it on purpose: the HTTP gate
(`app/api/middlewares/auth.py`) lists the few routes that answer for themselves and treats
everything else as needing a grant.

## What a grant says

A grant is a compact HS256-signed token (`header.payload.signature`, the usual JWT layout):

| Claim | Meaning |
|---|---|
| `aud` | the API it is for: `sync`, `watch`, `admin` or `authz` |
| `sub` | who is asking — `login@platform` for a CLI user, a service name otherwise |
| `tenant` | the tenant the call is scoped to (optional) |
| `platform` | the Git platform the call was authorised on (optional) |
| `datasource` | the datasource the call was authorised for (optional) |
| `iat`, `nbf`, `exp` | issue, not-before and expiry — all required |

The orchestrator checks the signature, the audience and the lifetime on every call, and the
claims then bound what the call may do:

- **The grant decides the tenant and platform**, never a header or the payload. A call for a
  different datasource, tenant or platform than the grant names is refused. For a CLI call the
  tenant in the grant is the one the gateway's permission engine checked: a declared tenant,
  allowed by the rule, that the caller's Git access reaches (an admin may act for any declared
  tenant). With internal authentication on, a CLI grant without a tenant is refused, and the
  `tenant_id` a request body carries is never copied into a grant.
- **A push's tenant is the one whose repositories it came from**, and a schema push's the one
  whose `schema_repo` it is; a tenant named by a grant or header is checked against that, never
  used in its place. A push no declared tenant owns is refused (`403`).
- **A tenant writes only its `destinations`.** Once any tenant lists destinations for a
  datasource, a sync to a place outside the tenant's own is refused before a sink is opened,
  and the sync lock is keyed by the destination so two tenants writing one place wait for each
  other.
- **The caller recorded with a task is the grant's `sub`.** The `x-cli-user` and
  `x-tenant-id` headers are ignored once authentication is on.
- **The pushed repository must be one the rule covers.** Independently of the grant, a
  webhook payload that names a repository outside the rule's `repositories` (or inside its
  `exclude_repositories`) is refused, because the payload is whatever its sender wrote. A
  rule that names no repositories covers the one its `repo_url` points at.

Lifetimes are short — `sync` and `watch` grants live at most 10 minutes (the gateway and
watcher mint one per call, valid for 60 seconds); an `admin` grant is a standing credential
for a scraper and may live up to 30 days.

## Turning it on

Give every service the **same** secret, at least 32 characters:

```bash
export INTERNAL_AUTH_SECRET="$(openssl rand -hex 32)"
```

| Service | Variable | Notes |
|---|---|---|
| orchestrator (API **and** workers) | `INTERNAL_AUTH_SECRET` or `INTERNAL_AUTH_SECRET_FILE` | verifies grants |
| gateway | `INTERNAL_AUTH_SECRET` or `INTERNAL_AUTH_SECRET_FILE` | signs `sync` grants |
| watcher | `INTERNAL_AUTH_SECRET` or `INTERNAL_AUTH_SECRET_FILE` | signs `watch` grants |
| gateway **and** orchestrator | `INTERNAL_AUTH_SECRET` or `INTERNAL_AUTH_SECRET_FILE` | the orchestrator signs `authz` grants, the gateway verifies them ([the permission engine](#the-permission-engine)) |

Prefer `*_FILE` pointing at a mounted secret over putting the value in the environment.

The watcher and the admin listener may have **separate** keys, so that holding one does not
let you mint the others: `INTERNAL_AUTH_WATCH_SECRET` (orchestrator and watcher) and
`INTERNAL_AUTH_ADMIN_SECRET` (orchestrator, and whoever mints the scrape token). Each falls
back to `INTERNAL_AUTH_SECRET` when unset.

Authentication is enforced on the orchestrator whenever `INTERNAL_AUTH_SECRET` is set there.
Turn it on everywhere at once: a gateway without the secret would send unsigned calls that an
orchestrator with it refuses.

### Production refuses to start without it

With `APP_ENV=production` the orchestrator, the gateway and the watcher all refuse to start
unless a secret is set. The gateway and the watcher also refuse a secret shorter than 32
characters, and a `*_FILE` that cannot be read, rather than running unsigned.

### The development switch

Without a secret the orchestrator runs **open**: every call is accepted, as before, and it
logs `internal authentication is OFF` at startup. This is for a laptop. The repository's
end-to-end demo (`e2e/docker-compose.yml`) runs with it **on**: one demo `INTERNAL_AUTH_SECRET`
(overridable with `VARTRACK_DEMO_INTERNAL_AUTH_SECRET`) on the gateway, the orchestrator, the
watcher and the test runner, which signs its own calls to the orchestrator; its Suite 18 sends the
calls that must be refused and checks each refusal. (The live UI stack, `e2e/docker-compose.live.yml`,
still runs open.) To run a production-mode service open on purpose, set `INTERNAL_AUTH_INSECURE=true`;
there is no other way to get a production orchestrator without authentication.

## Scraping the admin port

`/metrics` needs an `admin` grant once authentication is on. Mint one and give it to the
scraper:

```bash
docker compose exec orchestrator-api \
  python -m app.api.tokens mint --audience admin --subject prometheus --ttl 2592000 > admin.token
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: vartrack-orchestrator
    authorization:
      credentials_file: /etc/prometheus/admin.token
    static_configs:
      - targets: ["orchestrator-api:9093"]
```

Renew the token before it expires (`--ttl` is in seconds, at most 30 days). The orchestrator's
own `/v1/health/liveness` and `/v1/health/readiness` on the API port stay open and are the
right place for Kubernetes probes: the admin listener binds to **loopback** by default
(`ADMIN_HOST=127.0.0.1`), so a probe that targets the pod's address cannot reach it. Publish
the admin port deliberately with `ADMIN_HOST=0.0.0.0` only together with
`INTERNAL_AUTH_SECRET`.

## The permission engine

Who may do what is decided in one place: the **permission engine** in the gateway
([Authentication & Authorization](../configuration/auth.md)). The CLI API asks it in-process.
The orchestrator's web UI asks it over gRPC, so the two never disagree. This is the one place a
Git token has to reach, which is why the call is protected like the others.

| | |
|---|---|
| Served by | the gateway, on `AUTHZ_ADDR` (e.g. `:5658`); unset serves nothing |
| Asked by | the orchestrator API, with `GATEWAY_AUTHZ_ADDR=gateway:5658` |
| Needs | an `authz` grant on every call, signed with `INTERNAL_AUTH_AUTHZ_SECRET` (else `INTERNAL_AUTH_SECRET`), minted by the orchestrator |
| Carries | the user's Git tokens as **request fields**, never in the grant (a grant is signed, not encrypted) and never in a log |
| Without it | the web UI with sign-in on answers `503 permissions are unavailable`: there is no local fallback |

In production the listener is TLS with the gateway's own certificate (`GATEWAY_TLS_CERT`/`KEY`) and,
when `GRPC_TLS_CA` is set, requires a client certificate that CA signed (mTLS); the orchestrator
presents `TLS_CERT_FILE`/`TLS_KEY_FILE` and verifies the gateway with `GATEWAY_AUTHZ_CA_FILE` (else
`TLS_CA_FILE`). The gateway refuses to serve `AUTHZ_ADDR` in production without a signing secret.
Without a secret (development) it serves open and says so at startup. Only the orchestrator's API
needs to reach it: the NetworkPolicy in [`deploy/networkpolicy/gateway.yaml`](https://github.com/jarin-devoss/varTrack/tree/main/deploy/networkpolicy)
opens `5658` to it alone.

What the engine learns from the Git platforms is remembered for `AUTHZ_CACHE_TTL` (default 60 s) per
token, platform and question, keyed by a hash of the token. Signing out drops what it knows of the
session's tokens; a revoked permission keeps working for up to the TTL.

## The optional Prometheus listener

When `PROM_ENABLED=true` the orchestrator starts a second metrics server on `PROM_PORT`. It is
a separate listener from the admin port, listens on every interface, and is **not** covered by
grants: its only protection is its own basic auth (`PROM_BASIC_AUTH_USER` /
`PROM_BASIC_AUTH_PASS`, optionally with `PROM_ENABLE_TLS`). If you turn it on, set those, or
keep the port off the network (the [NetworkPolicy](#keep-the-network-closed-as-well) manifests
do not open it); the admin port's `/metrics` is the one that takes a grant.

## CORS

`CORS_ORIGINS=*` is honoured only while the orchestrator runs open for development. With
authentication on, or in production, a wildcard is ignored and logged as an error: list the
web UI's origin instead.

## Keep the network closed as well

Authentication is the control; the network is the second one. Only the gateway, the watcher
and the web UI's proxy need to reach the orchestrator, and only Prometheus needs the admin
port. [`deploy/networkpolicy/`](https://github.com/jarin-devoss/varTrack/tree/main/deploy/networkpolicy)
has Kubernetes `NetworkPolicy` manifests that say exactly that: label your pods as the
manifests expect, apply them, and everything else is denied by default.

## Troubleshooting

| You see | Cause |
|---|---|
| `401 a signed grant is required` on a webhook or schema call | the caller sent no `Authorization: Bearer <grant>` |
| `401 invalid grant: invalid signature` | the caller and the orchestrator hold different secrets |
| `401 invalid grant: token has expired` / `not valid yet` | the clocks of the two hosts differ by more than 30 seconds, or the grant is stale |
| `401 invalid grant: wrong audience` | a `watch` or `admin` grant was sent where a `sync` grant is needed (or the reverse) |
| `403 the grant was issued for another tenant / datasource` | the call asks for more than its grant allows |
| `403 no declared tenant owns …` | a push came from a repository no tenant's `repositories` covers (and the rule names no single tenant to take it) |
| `permission denied: tenant … is not one of the tenants of the … rule` / `… has no access to the repositories tenant … covers` | the caller named a tenant the rule or their Git access does not allow |
| `destination '…' is not one of tenant …'s on …` | the sync would write outside the tenant's `destinations` |
| `403 repository '…' is not covered by the rule` | the payload names a repository the rule's `repositories` do not include |
| gRPC `UNAUTHENTICATED` from the gateway | the gateway has no `INTERNAL_AUTH_SECRET`, or a different one |
| web UI answers `503 permissions are unavailable`; the orchestrator logs `the permission engine refused this service's credentials` | the gateway and the orchestrator hold different `INTERNAL_AUTH_SECRET` / `INTERNAL_AUTH_AUTHZ_SECRET`, or their clocks differ by more than 30 s |
| web UI answers `503 permissions are unavailable`; the orchestrator logs `GATEWAY_AUTHZ_ADDR is not set` or `unreachable` | set `GATEWAY_AUTHZ_ADDR` on the orchestrator and `AUTHZ_ADDR` on the gateway, and let the orchestrator reach it |
| orchestrator exits at startup with `production mode requires INTERNAL_AUTH_SECRET` | set it, or `INTERNAL_AUTH_INSECURE=true` to run open on purpose |
