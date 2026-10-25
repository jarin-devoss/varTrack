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

`vt` authenticates with your Git token — a GitHub or Gitea personal access token. What it may do is what that token may do in the rule's repositories: push access syncs, read access validates and dry-runs ([Authentication & Authorization](../configuration/auth.md)).

```bash
vt login --server https://gateway.example.com --token "$GIT_TOKEN"

# Or skip login entirely — set env vars
export VARTRACK_SERVER=https://gateway.example.com
export VARTRACK_TOKEN=<your Git token>
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
| `--platform` | — | Platform of the rule (for example `github`, `gitea-internal`); needed only when rules on several platforms write to the datasource |
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
| `--platform` | — | Platform of the rule; needed only when rules on several platforms write to the datasource |
| `--tenant` | from context | Tenant ID |
| `--format` | auto | File format override |
| `--json` | false | Print result as JSON |

---

### `vt validate-config`

Check a `vartrack.json` or a tenant's `bundle.json` before pushing it, with the code a sync reads it with. Exits with code 1 when there are errors.

```bash
vt validate-config vartrack.json
vt validate-config .gitea/vartrack.json
vt validate-config schemas/bundle.json --tenant payments
```

The file's name says what it is: `vartrack.json` (also under `.github/`, `.gitea/` or `.gitlab/`), `bundle.json` or `rules.json`.

| It reports | What it means |
|---|---|
| **errors** | what a sync rejects: invalid JSON, a value of the wrong type, a second rule for one platform and datasource, a rule `config.cue` does not have or a tenant may not layer |
| **warnings** | what a sync ignores: keys only `config.cue` sets, [removed options](../configuration/schema-registry.md#how-a-rule-is-built), `prune` that has no effect on a whole-object sink |
| **migrations** | options under a name that changed (`env_as_branch` → `branch_as_env`); a sync still reads them |

A `vartrack.json`'s `extends` presets are checked by name, not fetched. A `bundle.json`'s presets are fetched from the tenant's schema repository, as a sync does.

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--tenant` | from context | Tenant ID: whose rules and presets a `bundle.json` is checked against |
| `--json` | false | Print `errors`, `warnings` and `migrations` as JSON |

The caller needs a token any platform of the bundle accepts; the file names no rule, so no repository permission is asked.

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
| `VARTRACK_TOKEN` | Your Git token — skips `vt login` |
| `VARTRACK_TENANT` | Tenant ID |
| `VARTRACK_CONTEXT` | Named context to activate |

---

## CI/CD integration

See the [CI/CD Integration](cicd.md) page for GitHub Actions, GitLab CI, and other pipeline examples.
