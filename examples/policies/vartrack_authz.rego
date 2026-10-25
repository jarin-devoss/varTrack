package vartrack.authz

# OPA runs after the Git platform has had its say: by the time this policy is
# asked, the caller's own Git token has shown push access to the rule's
# repositories for a sync, and read access for anything else. This policy
# adds the rules Git permissions cannot express — which environment, which
# file path.
#
# input.user.permission is the caller's permission on the rule's
# repositories ("admin", "write" or "read"); input.user.admin is true for the
# "platform:login" entries of config.cue's ui.auth.admins.

# Default deny — all requests are blocked unless explicitly allowed.
default allow = false

# ── Anything but a sync (validate, task get/list): Git read access decided ───

allow {
    input.action != "sync"
}

# ── varTrack admins and repository admins may sync anywhere ──────────────────

allow {
    input.action == "sync"
    is_admin
}

# ── Writers may sync outside production, except secrets paths ────────────────

allow {
    input.action == "sync"
    is_writer
    not is_production
    not is_secret_path
}

# ── Dry runs write nothing: read access is enough, anywhere ──────────────────

allow {
    input.action == "sync"
    input.dry_run == true
}

# ── Helper rules ─────────────────────────────────────────────────────────────

is_production {
    startswith(input.env, "prod")    # production, prod-eu, …
}

is_secret_path {
    startswith(input.file_path, "configs/secrets/")
}

is_admin {
    input.user.admin == true
}

is_admin {
    input.user.permission == "admin"
}

is_writer {
    input.user.permission == "write"
}
