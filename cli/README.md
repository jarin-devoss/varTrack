# vt — VarTrack CLI

`vt` is the command-line interface for VarTrack. It lets you push any local config file directly to a datasource from your terminal or CI/CD pipeline — without needing a Git push. It runs the same ETL pipeline as a webhook: parse → CUE validate → write.

---

## Installation

### Pre-built binaries

Download for your platform from the [releases page](https://github.com/jarin-devoss/varTrack/releases), then move to a directory on your `PATH`.

### Build from source

Requires Go 1.21+.

```bash
cd cli
go build -o vt ./cmd

# Move to PATH
mv vt /usr/local/bin/vt          # Linux / macOS
move vt.exe C:\Windows\System32\ # Windows
```

---

## Authentication

### Interactive login — OIDC / SSO

Supported providers: Azure AD / Entra ID, Google, Okta, Keycloak, and any OIDC-compliant IdP.

```bash
# Azure AD
vt login \
  --server         https://gateway.example.com \
  --oidc-issuer    "https://login.microsoftonline.com/<tenant-id>/v2.0" \
  --oidc-client-id "<app-client-id>"

# Google
vt login \
  --server         https://gateway.example.com \
  --oidc-issuer    https://accounts.google.com \
  --oidc-client-id "<client-id>"
```

A browser window opens, you authenticate, and the token is saved automatically.

### CI/CD — static token

```bash
vt login \
  --server https://gateway.example.com \
  --token  "$VARTRACK_TOKEN"
```

Or skip `vt login` entirely and set environment variables — `vt` reads them on every invocation:

```bash
export VARTRACK_SERVER=https://gateway.example.com
export VARTRACK_TOKEN=eyJ...
```

---

## Commands

### sync

Push a local file to a datasource. Runs the full ETL pipeline: parse → CUE validate → write.

```bash
# Basic sync
vt sync \
  --file       configs/app.yaml \
  --datasource mongo \
  --env        staging

# Dry-run — validate the pipeline without writing anything
vt sync --file configs/app.yaml --datasource mongo --env staging --dry-run

# Block until the task completes (polls every 2 s, default timeout 5 m)
vt sync --file configs/app.yaml --datasource mongo --env production --wait

# Custom timeout and label
vt sync \
  --file       configs/app.yaml \
  --datasource mongo \
  --env        production \
  --wait \
  --timeout    10m \
  --label      "release/v2.3.1"

# Output raw JSON (useful in scripts)
vt sync --file configs/app.yaml --datasource mongo --env staging --json
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--file` | — | Path to the config file (required) |
| `--datasource` | — | Target datasource name (required) |
| `--env` | — | Deployment environment (required) |
| `--tenant` | (from context) | Tenant ID |
| `--dry-run` | false | Validate without writing |
| `--wait` | false | Block until task finishes |
| `--timeout` | 5m | Max wait time |
| `--label` | "" | Free-form label attached to the task |
| `--json` | false | Print raw JSON response |

---

### validate

Validate a file against the registered CUE schema without syncing anything. Exit code 1 on failure — useful for blocking bad PRs.

```bash
vt validate --file configs/app.yaml

# Specify the datasource to select the schema
vt validate --file configs/app.yaml --datasource mongo

# With tenant ID
vt validate --file configs/app.yaml --datasource mongo --tenant myapp
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--file` | — | Path to the config file (required) |
| `--datasource` | — | Datasource name (selects the CUE schema) |
| `--tenant` | (from context) | Tenant ID |
| `--format` | auto | File format override |
| `--json` | false | Print result as JSON |

---

### task

Inspect sync tasks.

```bash
vt task get <task-id>          # Get task status and result
vt task list                   # List recent tasks
vt task list --limit 50        # Increase the list size
vt task watch <task-id>        # Stream status until task reaches a terminal state
```

---

### bundle validate

Check a local CUE bundle file for required fields.

```bash
vt bundle validate ./config.cue
```

---

### config

Manage named contexts. Contexts let you switch between different servers (dev, staging, production) without re-authenticating.

```bash
vt config view                          # Show config (tokens redacted)

vt config set-context prod \
  --server https://prod-gateway.example.com \
  --token  "$PROD_TOKEN"

vt config use-context prod              # Switch active context

vt logout                               # Remove the active context
vt logout --context staging             # Remove a named context
```

---

### version

```bash
vt version
```

---

## Configuration

Settings are stored in `~/.config/vt/config.yaml`. Environment variables override the active context:

| Variable | Purpose |
|---|---|
| `VARTRACK_SERVER` | Gateway base URL |
| `VARTRACK_TOKEN` | Bearer token (skips OIDC login) |
| `VARTRACK_TENANT` | Tenant ID |
| `VARTRACK_CONTEXT` | Named context to activate |

---

## CI/CD usage

### GitHub Actions

```yaml
- name: Validate config on PR
  env:
    VARTRACK_SERVER: https://gateway.example.com
    VARTRACK_TOKEN:  ${{ secrets.VARTRACK_TOKEN }}
  run: vt validate --file configs/app.yaml --datasource mongo

- name: Sync config to staging
  env:
    VARTRACK_SERVER: https://gateway.example.com
    VARTRACK_TOKEN:  ${{ secrets.VARTRACK_TOKEN }}
    VARTRACK_TENANT: myapp
  run: |
    vt sync \
      --file        configs/app.yaml \
      --datasource  mongo \
      --env         staging \
      --wait

- name: Sync config to production
  run: |
    vt sync \
      --file        configs/app.yaml \
      --datasource  mongo \
      --env         production \
      --wait \
      --label       "${{ github.ref_name }}"
```

### GitLab CI

```yaml
deploy:staging:
  script:
    - vt sync --file configs/app.yaml --datasource mongo --env staging --wait
  environment: staging
```

---

## Supported file formats

| Extension | Format |
|---|---|
| `.yaml`, `.yml` | YAML |
| `.json` | JSON |
| `.toml` | TOML |
| `.env` | dotenv |
| `.ini` | INI |
| `.hcl` | HCL (Terraform-style) |
| `.properties` | Java properties |

Format is auto-detected from the extension. Use `--format` to override.

---

## Building

```bash
make build     # ./vt for current platform
make test      # go test ./...
make lint      # golangci-lint (must be installed separately)
make release   # dist/ with all platform binaries
make help      # list all targets
```

Version and commit are injected at build time:

```
Version   = git describe --tags (or "dev")
GitCommit = git rev-parse --short HEAD
```

---

## Access control

Authorization is enforced at the gateway level on every request:

1. **JWT / OIDC** — every request must carry a valid Bearer token
2. **Casbin RBAC** — roles control which actions are permitted
3. **OPA** — fine-grained context-aware policies (which environment, which datasource, which file path)

### Built-in roles

| Role | sync | validate | task get/list |
|---|:---:|:---:|:---:|
| `role:admin` | all envs | yes | yes |
| `role:operator` | non-prod only | yes | yes |
| `role:viewer` | — | yes | yes |

### OPA policy example

```rego
package vartrack.authz

default allow = false

# Operators can sync to any non-production environment
allow {
    input.action == "sync"
    not startswith(input.env, "prod")
    "role:operator" == input.user.groups[_]
}

# Admins have unrestricted access
allow {
    "role:admin" == input.user.groups[_]
}

# Dry-run is always allowed for operators
allow {
    input.action == "sync"
    input.dry_run == true
    "role:operator" == input.user.groups[_]
}
```

See [examples/policies/vartrack_authz.rego](../examples/policies/vartrack_authz.rego) and [examples/bundles/13_auth_oidc_rbac_opa.cue](../examples/bundles/13_auth_oidc_rbac_opa.cue) for complete examples.
