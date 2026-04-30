# vt — CLI Reference

`vt` is the command-line interface for varTrack. Push any local config file directly to a datasource from your terminal or CI/CD pipeline — without needing a Git push.

---

## Installation

### Download pre-built binary

Download from the [releases page](https://github.com/jarin-devoss/varTrack/releases) for your platform, then add to your `PATH`.

### Build from source

```bash
cd cli
go build -o vt ./cmd
mv vt /usr/local/bin/vt
```

---

## Authentication

### OIDC / SSO login

```bash
# Azure AD / Entra ID
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

A browser window opens for login. The token is saved automatically.

### Static token (for CI/CD)

```bash
vt login --server https://gateway.example.com --token "$VARTRACK_TOKEN"

# Or skip login entirely — set env vars
export VARTRACK_SERVER=https://gateway.example.com
export VARTRACK_TOKEN=eyJ...
```

---

## Commands

### `vt sync`

Push a local file to a datasource. Runs the full ETL pipeline.

```bash
# Basic sync
vt sync \
  --file       configs/app.yaml \
  --datasource mongo \
  --env        production

# Dry-run — see what would be written without touching the datasource
vt sync --file configs/app.yaml --datasource mongo --env staging --dry-run

# Wait for the task to complete
vt sync --file configs/app.yaml --datasource mongo --env production --wait

# Custom timeout and label
vt sync \
  --file       configs/app.yaml \
  --datasource mongo \
  --env        production \
  --wait       \
  --timeout    10m \
  --label      "release/v2.3.1"

# Output raw JSON
vt sync --file configs/app.yaml --datasource mongo --env staging --json
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--file` | — | Path to the config file (**required**) |
| `--datasource` | — | Target datasource name (**required**) |
| `--env` | — | Deployment environment (**required**) |
| `--tenant` | from context | Tenant ID |
| `--dry-run` | false | Validate without writing |
| `--wait` | false | Block until task completes |
| `--timeout` | 5m | Max wait time |
| `--label` | "" | Free-form label attached to the task |
| `--json` | false | Print raw JSON response |

---

### `vt validate`

Validate a file against the CUE schema without syncing. Exits with code 1 on failure — ideal for PR checks.

```bash
vt validate --file configs/app.yaml

# Specify the datasource to select the right schema
vt validate --file configs/app.yaml --datasource mongo

# With tenant
vt validate --file configs/app.yaml --datasource mongo --tenant myapp
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--file` | — | Path to the config file (**required**) |
| `--datasource` | — | Datasource name (selects the schema) |
| `--tenant` | from context | Tenant ID |
| `--format` | auto | File format override |
| `--json` | false | Print result as JSON |

---

### `vt task`

Inspect sync tasks created by `vt sync`.

```bash
vt task get  <task-id>          # Get status and result
vt task list                    # List recent tasks
vt task list --limit 50
vt task watch <task-id>         # Stream status until terminal state
```

---

### `vt bundle validate`

Check a CUE bundle file for required fields.

```bash
vt bundle validate ./config.cue
```

---

### `vt config`

Manage named contexts — useful when you have multiple servers (dev, staging, production).

```bash
vt config view                          # Print config (tokens redacted)

vt config set-context prod \
  --server https://prod-gateway.example.com \
  --token  "$PROD_TOKEN"

vt config use-context prod              # Switch active context

vt logout                               # Remove the active context
vt logout --context staging
```

---

### `vt version`

```bash
vt version
```

---

## Environment variables

| Variable | Purpose |
|---|---|
| `VARTRACK_SERVER` | Gateway base URL |
| `VARTRACK_TOKEN` | Bearer token — skips `vt login` |
| `VARTRACK_TENANT` | Tenant ID |
| `VARTRACK_CONTEXT` | Named context to activate |

---

## CI/CD integration

See the [CI/CD Integration](cicd.md) page for GitHub Actions, GitLab CI, and other pipeline examples.
