# Authentication & Authorization

varTrack's CLI API (`/v1/cli/*`, used by `vt`) keeps no users or roles of its own. The caller shows their own Git token, and the rule's Git platform says what they may do. An optional **OPA** policy then decides with the whole request.

---

## Git permissions

`vt` sends the caller's Git token for the rule's platform — a GitHub or Gitea personal access token — as `Authorization: Bearer <token>`:

```bash
vt login --server https://gateway.example.com --token <your Git token>
```

For each request the gateway asks the platform of the rule that syncs to the datasource who the token belongs to (`GET /user`) and what they may do in the rule's repositories (`GET /repos/{owner}/{repo}`, and `GET /user/repos` for patterns such as `acme/*`):

| The caller can, in one of the rule's repositories… | …so they may |
|---|---|
| push | `vt sync` |
| read | `vt sync --dry-run`, `vt validate`, `vt task` |

A rule's repositories are the ones its `repositories` and enabled `overrides` name or match, minus its exclusions — the repositories the gateway routes pushes from. The web UI decides the same way (see below).

| Answer | When |
|---|---|
| `401` | no token, or the platform rejects it |
| `403` | the caller lacks the access above, or the OPA policy denies the request |
| `404` | no rule syncs to the datasource; a task of a rule the caller cannot read |

`vt validate` without `--datasource`, and `vt validate-config`, need a token any platform of the bundle accepts. `vt task list` shows the tasks of the rules the caller can read.

The `platform:login` entries of [`ui.auth.admins`](#set-it-up) may do anything, on the CLI API as in the UI. Tasks record their caller as `login@platform`.

The bundle's `auth.oidc` and `auth.rbac` blocks are no longer used; the gateway logs a warning when they are set.

---

## OPA — policy layer (Open Policy Agent)

OPA runs after the Git permission check for decisions Git permissions can't express — e.g., "writers may sync anywhere but production". Give the policy in exactly one of three ways:

```cue
auth: {
  opa: {
    inline_policy: """
      package vartrack.authz

      default allow = false

      # validate and task reads: Git read access has decided
      allow {
        input.action != "sync"
      }

      # anyone who may push syncs outside production
      allow {
        input.action == "sync"
        input.env    != "production"
      }

      # only repository admins sync production
      allow {
        input.action == "sync"
        input.env    == "production"
        input.user.permission == "admin"
      }
    """

    // policy_file: "/etc/vartrack/policy.rego"                      // a file on the gateway
    // server_url:  "http://opa:8181/v1/data/vartrack/authz/allow"  // a remote OPA server
  }
}
```

The policy must be in package `vartrack.authz` and define `allow`; anything but `allow == true` denies the request. Policies are written in Rego v0 syntax (`allow { … }`), as above. To write Rego v1 syntax (`allow if { … }`, `x in xs`), start the policy with `import rego.v1`.

`inline_policy` and `policy_file` are compiled in-process (tens of microseconds per decision) — no OPA server required. With `server_url` the gateway asks that server instead: point it at the rule (`…/v1/data/vartrack/authz/allow`) or at the package (`…/v1/data/vartrack/authz`, whose `allow` is then read). A server that cannot be reached, answers anything but 2xx, or takes longer than five seconds denies the request with `500`. A policy that does not compile, or a policy file that cannot be read, stops the gateway at startup.

A full example policy — admins sync anywhere, writers outside production but never `configs/secrets/*`, dry runs anywhere — is at [`examples/policies/vartrack_authz.rego`](https://github.com/jarin-devoss/varTrack/blob/main/examples/policies/vartrack_authz.rego).

### OPA input document

Every request evaluation receives:

| Field | Example |
|---|---|
| `input.user.login` | `"alice"` — on the rule's platform |
| `input.user.platform` | `"gitea"` |
| `input.user.permission` | `"admin"`, `"write"` or `"read"` — in the rule's repositories |
| `input.user.admin` | `true` for `ui.auth.admins` entries |
| `input.user.repositories` | `["acme/configs"]` — the rule's repository names and patterns |
| `input.action` | `"sync"`, `"validate"` |
| `input.resource` | `"datasource"` |
| `input.datasource` | `"mongo"` |
| `input.env` | `"production"` (lower-cased) |
| `input.file_path` | `"configs/app.yaml"` |
| `input.tenant_id` | `"acme"` |
| `input.dry_run` | `false` |

### Reloading the policy

`POST /admin/opa/reload` on the gateway's admin port (`ADMIN_ADDR`, default `:9090`) recompiles the policy and re-reads `policy_file`. If the new policy does not compile, the call answers `422` with the compile error and the previous policy stays in force. A `policy_file` is also re-read every `OPA_RELOAD_INTERVAL` (default `30m`).

---

## Layer evaluation order

```
Request
  │
  ▼
Git token             ← 401 if missing, or the rule's platform rejects it
  │
  ▼
Git permission        ← 403 without push (sync) or read (anything else) access
  │
  ▼
OPA evaluation        ← 403 if the policy does not allow it
  │
  ▼
Handler
```

OPA is skipped if no `opa` block is configured.

---

## Web UI sign-in with your Git platform

The web UI can sign users in with the Git platforms your rules already use (GitHub, GitHub Enterprise, Gitea) and show each user only what those platforms let them see. varTrack keeps no list of who may see what: each rule is judged on its own platform, asked with the user's own account there.

| The user can, on the rule's platform… | …so in the UI they |
|---|---|
| read at least one repository the rule covers | see the rule, its tasks and its events |
| read the repository an environment was last synced from | see that environment, its keys and its diff |
| write to one of the rule's repositories | can **Sync** it; with read access only they can **Dry run** it |

A rule covers the repositories it names, the ones its patterns (`acme/*`) match minus its exclusions, and the ones its environments were synced from. A rule, environment or task the user cannot see answers *not found*, exactly like one that does not exist. Answers are cached for two minutes per session, so a permission change on a platform shows up within that time.

### Tenants are shown, not chosen

The UI covers **every tenant at once**: a rule, task or event carries its tenant, and what a person sees is what Git lets them read, whichever tenant it belongs to. There is no tenant to switch to.

| Who | Sees |
|---|---|
| Sign-in is off | every rule of every tenant |
| An admin (`ui.auth.admins`) | every rule of every tenant |
| A signed-in user | every rule, of any tenant, whose repository their account can read on the rule's platform. A tenant with no such rule does not appear at all. |

When the viewer sees more than one tenant, the Rules, Environments, Events and Tasks pages show the tenant and offer a Tenant filter that lists only those tenants. The same `platform/datasource` can exist in two tenants, so a rule is identified by its tenant too: `/rules/{tenant}/{platform}/{datasource}`; a link without the tenant still opens the rule when only one visible tenant has it.

A rule you cannot see because you have not connected its platform is counted, never named: the Rules page says "3 rules on gitea are hidden" with a **Connect gitea** button. Rules on a platform you are connected to, in repositories your account cannot read, are not counted — that would show that a private repository's rules exist.

The UI API takes an optional `?tenant=` (or the `X-Tenant-ID` header) to narrow an answer to one tenant. It narrows only: it never shows a rule the viewer cannot read.

### Rules on several platforms

With rules on GitHub and on Gitea, add a provider for each. Users sign in with either one and **connect** the other from the sign-in page, the user menu or their profile — the same session then holds both accounts, and each rule is judged with the account on its own platform. A GitHub rule is never judged by a Gitea account, even when the repository names match. Until a platform is connected, its rules stay hidden and the UI says how many there are. Signing out ends the session on every platform. Rules on a platform without sign-in set up are hidden from everyone but admins.

### Set it up

1. Register an OAuth application on each platform, all with the same callback URL: `{public_url}/api/orchestrator/v1/auth/callback` — for example `http://localhost:8080/api/orchestrator/v1/auth/callback`.
    - **GitHub:** *Settings → Developer settings → OAuth Apps*. GitHub OAuth Apps can only read private repositories with the `repo` scope, which is the default here.
    - **Gitea:** *Settings → Applications → OAuth2 Applications*, confidential client. Default scopes: `read:user read:repository`.
2. Add a `ui` block to the bundle (`config.cue`), with one provider per platform users sign in with:

```cue
bundle: {
  platforms: [
    {github: {endpoint: "https://github.com", ...}},
    {gitea:  {endpoint: "http://gitea:3000", ...}},
  ]

  ui: {
    public_url: "https://vartrack.example.com"   // where users open the UI
    auth: {
      providers: [
        {
          platform:      "github"                // a platform's name from platforms
          client_id:     "Iv1.8a61f9b3a7aba766"
          client_secret: {file: "/run/secrets/github-oauth"}
        },
        {
          platform:      "gitea"
          client_id:     {file: "/run/secrets/gitea-oauth-id"}
          client_secret: {ref: {path: "ui/gitea", key: "client_secret"}}   // from Vault
          browser_url:   "https://gitea.example.com"   // when the platform's endpoint is internal
        },
      ]
      admins: ["github:octo-admin"]              // optional: see and sync every rule
      secret_manager: "vault"                    // optional with a single secret manager
    }
  }
}
```

| Field | Default | Description |
|---|---|---|
| `ui.public_url` | `http://localhost:5173` | Where users open the UI; the callback is built from it |
| `ui.auth.providers[].platform` | — | A platform's name from `platforms`: `github`, `gitea`, `gitea-team`… |
| `ui.auth.providers[].client_id` / `client_secret` | — | The OAuth application: a string, `{value: "…"}`, `{file: "/path"}` or `{ref: {path: "…", key: "…"}}` |
| `ui.auth.providers[].browser_url` | the platform's `endpoint` | The platform's address as the browser sees it, when the endpoint is internal (e.g. `http://gitea:3000`) |
| `ui.auth.providers[].scopes` | per platform, see above | OAuth scopes |
| `ui.auth.admins` | — | `platform:login` entries that see every rule; with one provider a bare login works. With several, an admin must name the platform: `alice` on GitHub and `alice` on Gitea need not be the same person |
| `ui.auth.session_ttl_seconds` | `28800` | How long a session lasts (8 hours) |
| `ui.auth.session_secret` | derived from the client secrets | Key for encrypting sessions at rest |
| `ui.auth.redirect_url` | `{public_url}/api/orchestrator/v1/auth/callback` | The callback URL, when it differs |
| `ui.auth.secret_manager` | the bundle's only secret manager | Which entry of [`secret_managers`](bundle.md#secret_managers) `{ref: …}` values come from, by name: `vault`, or `vault-{tag}` for a tagged one. Needed when there are several |

Without `ui.auth` the UI is open to anyone who can reach it, as before. If sign-in is set up but cannot work — a missing secret, a file that can't be read, an unknown platform — the UI stays closed and says it is misconfigured; the orchestrator's log says why.

`{ref: {path, key}}` reads field `key` of the Vault secret at `path` (under the secret manager's `mount_point`). The orchestrator reads files and references when it loads these settings, then again at most once a minute, so a rotated secret is picked up without a restart. If Vault can't be reached or the key is missing, the UI stays closed and the orchestrator's log names the field. Rotating a client secret also ends the sessions in progress unless `session_secret` is set, since the session key is otherwise derived from the client secrets. The model is [`ui.proto`](https://github.com/jarin-devoss/varTrack/blob/main/proto/vartrack/v1/models/ui.proto).

**Without a `ui.auth` block**, the orchestrator reads the same settings from environment variables: `UI_AUTH_PLATFORMS` (`github,gitea`), then per platform `UI_AUTH_{P}_CLIENT_ID`, `UI_AUTH_{P}_CLIENT_SECRET`, `UI_AUTH_{P}_URL` and `UI_AUTH_{P}_SCOPES` (`{P}` is the platform name upper-cased with `-` as `_`), plus `UI_PUBLIC_URL`, `UI_AUTH_REDIRECT_URL`, `UI_AUTH_ADMINS`, `UI_SESSION_TTL_SECONDS` and `UI_SESSION_SECRET`. Any of them can be read from a file by appending `_FILE`.

### How sessions work

Sign-in is the OAuth2 authorization code flow with PKCE. The login state is single-use, expires after five minutes, names the platform, and is bound to the browser that started it by a cookie. After sign-in the browser holds only a random session id in an `HttpOnly` cookie (`Secure` when the UI is served over HTTPS). The platform tokens stay on the server, encrypted with AES-256-GCM, in Redis next to the sync ledger, and expired tokens are refreshed when the platform issues refresh tokens. Signing out deletes the session on the server, so a copied cookie stops working too.

This covers the web UI's API (`/v1/ui/*`). The CLI API (`/v1/cli/*`, used by `vt` and by the UI's Playground) takes a Git token instead, as above; paste one under *API token for the Playground* on the sign-in page to use it from the UI.
