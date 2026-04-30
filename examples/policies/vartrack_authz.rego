package vartrack.authz

# Default deny — all requests are blocked unless explicitly allowed.
default allow = false

# ── Allow non-sync actions for operators and above ────────────────────────────

allow {
    input.action != "sync"
    is_operator_or_above
}

# ── Allow sync to non-production environments for operators ───────────────────

allow {
    input.action == "sync"
    not is_production
    is_operator_or_above
}

# ── Production sync requires admin role ───────────────────────────────────────

allow {
    input.action == "sync"
    is_production
    is_admin
}

# ── Dry-run syncs are always allowed for operators (no writes happen) ─────────

allow {
    input.action == "sync"
    input.dry_run == true
    is_operator_or_above
}

# ── Block writes to secrets paths for non-admins ─────────────────────────────

deny_secret_path {
    startswith(input.file_path, "configs/secrets/")
    not is_admin
}

allow {
    not deny_secret_path
    input.action == "sync"
    is_admin
}

# ── Helper rules ─────────────────────────────────────────────────────────────

is_production {
    input.env == "production"
}

is_production {
    startswith(input.env, "prod")
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
