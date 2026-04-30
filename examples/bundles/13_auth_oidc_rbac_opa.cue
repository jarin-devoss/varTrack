// Example 13 — Full auth stack: OIDC + Casbin RBAC + OPA
//
// This bundle enables the vtctl CLI API (/v1/cli/*) with:
//   - OIDC login via Azure AD / Entra ID (or any OIDC provider)
//   - Casbin RBAC: role:admin / role:operator / role:viewer
//   - OPA policy: blocks production syncs for non-admins
//
// vtctl login command:
//   vtctl login \
//     --server https://gateway.example.com \
//     --oidc-issuer https://login.microsoftonline.com/<tenant-id>/v2.0 \
//     --oidc-client-id <azure-app-client-id>
//
// CI/CD (static token, no browser):
//   vtctl login --server https://gateway.example.com --token $VARTRACK_TOKEN

package vartrack

platform: {
    github: {
        endpoint:    "https://github.example.com"
        webhook_secret: "my-webhook-secret"
    }
}

datasources: {
    "mongo-primary": {
        url: "webhooks/mongo"
        connection: {
            uri: "mongodb://mongo:27017"
            db:  "vartrack"
        }
    }
}

rules: [{
    datasource: "mongo-primary"
    files:      ["configs/*.yaml"]
    root_key:   "app"
}]

schema_registry: {
    repo:   "https://github.com/my-org/vartrack-schemas"
    branch: "main"
    token:  "ghp_..."
}

// ── Auth block — enables vtctl CLI ───────────────────────────────────────────

auth: {
    oidc: {
        // Azure AD / Entra ID — replace <tenant-id> and <client-id>.
        // For Google: https://accounts.google.com
        // For Okta:   https://your-org.okta.com
        issuer_url:    "https://login.microsoftonline.com/<tenant-id>/v2.0"
        client_id:     "<azure-app-client-id>"
        audience:      "<azure-app-client-id>"
        groups_claim:  "groups"
        extra_scopes:  ["https://graph.microsoft.com/.default"]
    }

    rbac: {
        // Casbin policy CSV — who maps to which role.
        //
        // Permissions (built-in defaults, shown for reference):
        //   p, role:admin,    *,          *,         allow
        //   p, role:operator, datasource, sync,      allow
        //   p, role:operator, datasource, validate,  allow
        //   p, role:operator, task,       get,       allow
        //   p, role:viewer,   datasource, validate,  allow
        //   p, role:viewer,   task,       get,       allow
        //
        // Role assignments (use email addresses or Azure AD group object IDs):
        policy: """
            g, alice@example.com,              role:admin
            g, aaaaaaaa-bbbb-cccc-dddd-000000, role:operator
            g, everyone,                        role:viewer
        """

        default_role: "role:viewer"
    }

    opa: {
        // Inline Rego — compiled in-process at gateway startup (~50µs eval, no extra process).
        // Rules:
        //   - Operators can sync to staging/dev but NOT production
        //   - Admins can sync anywhere
        //   - Dry-run is always allowed for operators (no data written)
        //   - Nobody touches configs/secrets/* except admins
        inline_policy: """
            package vartrack.authz

            default allow = false

            # Non-sync actions: operators and above
            allow {
                input.action != "sync"
                is_operator_or_above
            }

            # Sync to non-production: operators and above
            allow {
                input.action == "sync"
                not is_production
                is_operator_or_above
            }

            # Sync to production: admins only
            allow {
                input.action == "sync"
                is_production
                is_admin
            }

            # Dry-run syncs: always allowed for operators (no data written)
            allow {
                input.action == "sync"
                input.dry_run == true
                is_operator_or_above
            }

            # Block secrets path for non-admins
            allow {
                not startswith(input.file_path, "configs/secrets/")
                input.action == "sync"
                is_operator_or_above
            }

            is_production {
                input.env == "production"
            }
            is_production {
                startswith(input.env, "prod-")
            }

            is_admin {
                "role:admin" == input.user.groups[_]
            }
            is_operator_or_above {
                is_admin
            }
            is_operator_or_above {
                "role:operator" == input.user.groups[_]
            }
        """

        // To use a file instead (reload without restart via /admin/opa/reload):
        // policy_file: "/etc/vartrack/policy.rego"

        // To delegate to a remote OPA server:
        // server_url: "http://opa:8181/v1/data/vartrack/authz/allow"
    }
}
