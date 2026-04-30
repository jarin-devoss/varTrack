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
