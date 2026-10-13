# Authentication & Authorization

varTrack's CLI API (`/v1/cli/*`) is secured by a three-layer auth stack: **OIDC** for identity, **RBAC** for coarse-grained roles, and **OPA** for fine-grained policy. All three are optional — when absent, CLI routes are disabled.

---

## Enabling auth

Auth is configured in the bundle under `auth`:

```cue
bundle: {
  auth: {
    oidc: {
      issuer_url: "https://accounts.google.com"
      client_id:  "my-client-id"
    }
    rbac: {
      policy: """
        p, role:admin,    *, *,         allow
        p, role:operator, datasource, sync,     allow
        p, role:operator, datasource, validate, allow
        p, role:operator, task,       get,      allow
        p, role:viewer,   datasource, validate, allow
        p, role:viewer,   task,       get,      allow

        g, alice@myorg.com, role:admin
        g, bob@myorg.com,   role:operator
      """
      default_role: "role:viewer"
    }
  }
}
```

---

## OIDC — identity layer

varTrack validates the `Authorization: Bearer <token>` header against your OIDC provider. Any compliant provider works.

```cue
auth: {
  oidc: {
    issuer_url:   "https://accounts.google.com"  // required
    client_id:    "my-client-id"                 // required
    audience:     ""          // optional — defaults to client_id
    extra_scopes: []          // optional additional OAuth2 scopes
    groups_claim: "groups"    // JWT claim for group memberships
  }
}
```

**Supported providers:** Google, Azure AD / Entra ID, Okta, Auth0, Keycloak, GitLab, any OIDC-compliant provider.

The CLI logs in via `vt login` and stores the token locally. The token is sent as a Bearer header on every request.

---

## RBAC — role layer (Casbin)

Casbin RBAC maps users and groups to roles, and roles to allowed actions.

```cue
auth: {
  rbac: {
    policy: """
      // Format: p, <role>, <resource>, <action>, allow
      p, role:admin,    *,          *,        allow
      p, role:operator, datasource, sync,     allow
      p, role:operator, datasource, validate, allow
      p, role:operator, task,       get,      allow
      p, role:viewer,   datasource, validate, allow
      p, role:viewer,   task,       get,      allow

      // Format: g, <email-or-group>, <role>
      g, alice@myorg.com,   role:admin
      g, bob@myorg.com,     role:operator
      g, platform-team,     role:operator    // group from groups_claim
    """
    default_role: "role:viewer"              // for users with no explicit assignment
  }
}
```

**Resources:** `datasource`, `task`, `bundle`, `watcher`

**Actions:** `sync`, `validate`, `get`, `list`, `heal`

The `default_role` applies to any authenticated user who has no explicit role assignment.

---

## OPA — policy layer (Open Policy Agent)

OPA runs after RBAC for fine-grained decisions that RBAC can't express — e.g., "only allow syncing to `production` if the user's group is `platform-team`".

```cue
auth: {
  opa: {
    policy: """
      package vartrack.authz

      default allow = false

      allow {
        input.action == "sync"
        input.env    != "production"   // anyone can sync non-prod
      }

      allow {
        input.action == "sync"
        input.env    == "production"
        "platform-team" in input.user.groups   // only platform-team can sync prod
      }

      allow {
        input.action == "validate"     // everyone can validate
      }
    """
  }
}
```

### OPA input document

Every request evaluation receives:

| Field | Example |
|---|---|
| `input.user.sub` | `"alice@myorg.com"` |
| `input.user.email` | `"alice@myorg.com"` |
| `input.user.groups` | `["platform-team", "sre"]` |
| `input.action` | `"sync"` |
| `input.resource` | `"datasource"` |
| `input.datasource` | `"mongo"` |
| `input.env` | `"production"` |
| `input.file_path` | `"configs/app.yaml"` |
| `input.tenant_id` | `"acme"` |
| `input.dry_run` | `false` |

OPA runs embedded (in-process, ~50µs) — no external OPA server required. To reload a policy without restarting, call `POST /admin/opa/reload`.

A full example policy is at [`examples/policies/vartrack_authz.rego`](../../examples/policies/vartrack_authz.rego).

---

## Layer evaluation order

```
Request
  │
  ▼
OIDC validation       ← 401 if token invalid or expired
  │
  ▼
RBAC check            ← 403 if role doesn't allow action
  │
  ▼
OPA evaluation        ← 403 if policy returns false
  │
  ▼
Handler
```

All three must pass. OPA is skipped if no `opa` block is configured.

---

## Web UI sign-in with your Git platform

The web UI can sign users in with the Git platforms your rules already use (GitHub, GitHub Enterprise, Gitea) and show each user only what those platforms let them see. varTrack keeps no list of who may see what: each rule is judged on its own platform, asked with the user's own account there.

| The user can, on the rule's platform… | …so in the UI they |
|---|---|
| read at least one repository the rule covers | see the rule, its tasks and its events |
| read the repository an environment was last synced from | see that environment, its keys and its diff |
| write to one of the rule's repositories | can **Sync** it; with read access only they can **Dry run** it |

A rule covers the repositories it names, the ones its patterns (`acme/*`) match minus its exclusions, and the ones its environments were synced from. A rule, environment or task the user cannot see answers *not found*, exactly like one that does not exist. Answers are cached for two minutes per session, so a permission change on a platform shows up within that time.

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

Without `ui.auth` the UI is open to anyone who can reach it, as before. If sign-in is set up but cannot work — a missing secret, a file that can't be read, an unknown platform — the UI stays closed and says why.

`{ref: {path, key}}` reads field `key` of the Vault secret at `path` (under the secret manager's `mount_point`). The orchestrator reads files and references when it loads these settings, then again at most once a minute, so a rotated secret is picked up without a restart. If Vault can't be reached or the key is missing, the UI stays closed and names the field. Rotating a client secret also ends the sessions in progress unless `session_secret` is set, since the session key is otherwise derived from the client secrets. The model is [`ui.proto`](https://github.com/jarin-devoss/varTrack/blob/main/proto/vartrack/v1/models/ui.proto).

**Without a `ui.auth` block**, the orchestrator reads the same settings from environment variables: `UI_AUTH_PLATFORMS` (`github,gitea`), then per platform `UI_AUTH_{P}_CLIENT_ID`, `UI_AUTH_{P}_CLIENT_SECRET`, `UI_AUTH_{P}_URL` and `UI_AUTH_{P}_SCOPES` (`{P}` is the platform name upper-cased with `-` as `_`), plus `UI_PUBLIC_URL`, `UI_AUTH_REDIRECT_URL`, `UI_AUTH_ADMINS`, `UI_SESSION_TTL_SECONDS` and `UI_SESSION_SECRET`. Any of them can be read from a file by appending `_FILE`.

### How sessions work

Sign-in is the OAuth2 authorization code flow with PKCE. The login state is single-use, expires after five minutes, names the platform, and is bound to the browser that started it by a cookie. After sign-in the browser holds only a random session id in an `HttpOnly` cookie (`Secure` when the UI is served over HTTPS). The platform tokens stay on the server, encrypted with AES-256-GCM, in Redis next to the sync ledger, and expired tokens are refreshed when the platform issues refresh tokens. Signing out deletes the session on the server, so a copied cookie stops working too.

This covers the web UI's API (`/v1/ui/*`). The CLI API (`/v1/cli/*`, used by `vt` and by the UI's Playground) keeps the OIDC, RBAC and OPA layers above; paste a token under *API token for the Playground* on the sign-in page to use it from the UI.
