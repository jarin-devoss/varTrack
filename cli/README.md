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

`vt` authenticates with your Git token — a GitHub or Gitea personal access token. The gateway asks the rule's Git platform what that token may do in the rule's repositories: push access syncs, read access validates, dry-runs and reads tasks.

### Log in

```bash
vt login \
  --server https://gateway.example.com \
  --token  "$GIT_TOKEN"
```

Or skip `vt login` entirely and set environment variables — `vt` reads them on every invocation:

```bash
export VARTRACK_SERVER=https://gateway.example.com
export VARTRACK_TOKEN=<your Git token>
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
| `--platform` | — | Platform of the rule (for example `github`, `gitea-internal`); needed only when rules on several platforms write to the datasource |
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
| `--platform` | — | Platform of the rule; needed only when rules on several platforms write to the datasource |
| `--tenant` | (from context) | Tenant ID |
| `--format` | auto | File format override |
| `--json` | false | Print result as JSON |

---

### validate-config

Check a `vartrack.json` or a tenant's `bundle.json` before pushing it, the way a sync would read it. Reports errors, warnings and migrations; exit code 1 on errors or warnings (`--strict`: also on migrations). With no file named it checks the `vartrack.json` files in the current directory, and has nothing to do when there are none.

```bash
vt validate-config
vt validate-config --strict
vt validate-config vartrack.json
vt validate-config schemas/bundle.json --tenant payments
```

| Flag | Default | Description |
|---|---|---|
| `--tenant` | (from context) | Tenant ID: whose rules and presets a `bundle.json` is checked against |
| `--strict` | false | Also fail when a file has migrations (options under a name that changed) |
| `--json` | false | Print `[{file, errors, warnings, migrations}]` as JSON |

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
| `VARTRACK_TOKEN` | Your Git token (skips `vt login`) |
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

Authorization is enforced at the gateway on every request:

1. **Git permissions** — the caller's Git token must have push access to one of the rule's repositories to sync, read access for anything else
2. **OPA** (optional) — context-aware policies: which environment, which datasource, which file path. The policy sees the caller's permission as `input.user.permission` (`admin`, `write` or `read`)

Details: [Authentication & Authorization](../docs/configuration/auth.md).

See [examples/policies/vartrack_authz.rego](../examples/policies/vartrack_authz.rego) and [examples/bundles/13_cli_auth_opa.cue](../examples/bundles/13_cli_auth_opa.cue) for complete examples.
