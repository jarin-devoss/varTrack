# Environment Resolution

When a webhook fires, varTrack resolves a single environment string from the event. This string is used to:

- Resolve `{env}` in `destination_template`
- Select the correct subtree from env-keyed config files
- Determine which sink collection, prefix, or path to write to

---

## Resolution order

varTrack tries each strategy in order and uses the first one that produces a match:

| Priority | Strategy | Example |
|---|---|---|
| 1 | `branch_map[branch]` | `main` → `production` |
| 2 | `file_path_map[file_path]` | `configs/prod/**` → `production` |
| 3 | `env_as_pr: true` | PR #42 → `pr-42` |
| 4 | `env_as_branch: true` | branch `develop` → `develop` |
| 5 | `env_as_tags: true` | tag `v1.2.3` → `v1.2.3` |
| 6 | fallback | always → `default` |

---

## `branch_map` — explicit mapping

Map specific branch names to environment names:

```cue
rules: [{
  platform:   "github"
  datasource: "mongo"
  branch_map: {
    main:    "production"
    develop: "staging"
    qa:      "qa"
  }
}]
```

Pushes to `main` write to `production-config`. Pushes to `develop` write to `staging-config`. Any branch not in the map falls through to the next strategy.

---

## `file_path_map` — path glob mapping

Map file paths (glob patterns) to environment names:

```cue
rules: [{
  platform:   "github"
  datasource: "mongo"
  file_path_map: {
    "configs/production/**": "production"
    "configs/staging/**":    "staging"
  }
}]
```

A push that changes `configs/production/app.yaml` resolves to `production`.

---

## `env_as_pr` — PR environments

```cue
env_as_pr: true
```

Pull request events resolve to `pr-{number}`. Useful for ephemeral preview environments.

```
PR #42  → env = "pr-42"
PR #103 → env = "pr-103"
```

---

## `env_as_branch` — branch name as environment

```cue
env_as_branch: true
```

The branch name becomes the environment name directly:

```
branch "develop"     → env = "develop"
branch "feature/foo" → env = "feature/foo"
```

---

## `env_as_tags` — tag name as environment

```cue
env_as_tags: true
```

Tag push events use the tag name as the environment:

```
tag "v1.2.3"    → env = "v1.2.3"
tag "release-5" → env = "release-5"
```

---

## `root_key` — extracting a subtree before slicing

If your config file has a wrapper key, set `root_key` in the rule to unwrap it first. varTrack extracts the subtree under that key, then runs env auto-detection on it.

```cue
rules: [{
  root_key: "vartrack"   // default — extract the "vartrack" subtree if it exists
}]
```

```yaml
# configs/app.yaml
vartrack:
  production:
    database_host: mongo.prod.internal
  staging:
    database_host: mongo.staging.internal
other_tool:
  setting: ignored      # not under root_key — never touched
```

`root_key: "vartrack"` → extracts the inner dict → Pattern 1 runs on it → `database_host=mongo.prod.internal` for `production`.

Set `root_key: ""` to always process the whole file regardless of structure.

If the key doesn't exist in the file, varTrack soft-falls back to the whole file.

---

## Auto-detection from config file structure

When your config file itself is keyed by environment, varTrack auto-detects the structure and extracts only the relevant slice — no extra rule config needed. Three patterns are supported.

---

### Pattern 1 — all top-level keys are env names

Every top-level value is a dict. varTrack merges `default` first, then the matching env overrides it.

```yaml
# configs/app.yaml
production:
  database_host: mongo.prod.internal
  max_connections: 100
staging:
  database_host: mongo.staging.internal
  max_connections: 10
default:
  max_connections: 5    # fallback for unknown envs
  log_level: info
```

| env | result |
|---|---|
| `production` | `database_host=mongo.prod.internal`, `max_connections=100`, `log_level=info` |
| `staging` | `database_host=mongo.staging.internal`, `max_connections=10`, `log_level=info` |
| `pr-42` | `max_connections=5`, `log_level=info` (from `default`) |

---

### Pattern 2 — per-field env sub-keys mixed with scalars

Each field that varies per env has a dict of env→value. Fields that are the same across all envs are plain scalars.

```yaml
# configs/app.yaml
database_host:
  production: mongo.prod.internal
  staging:    mongo.staging.internal
  default:    mongo.local
max_connections: 50     # same for every env — written as-is
log_level: info
```

| env | result |
|---|---|
| `production` | `database_host=mongo.prod.internal`, `max_connections=50`, `log_level=info` |
| `staging` | `database_host=mongo.staging.internal`, `max_connections=50`, `log_level=info` |
| `pr-42` | `database_host=mongo.local`, `max_connections=50`, `log_level=info` |

---

### Pattern 3 — env dicts and scalar defaults at the same level

Some keys at the same nesting level are env names (dicts with overrides), others are plain scalars that apply to every env. Order in the file doesn't matter — varTrack categorizes by value type, not position. The env dict is merged on top of the scalars.

```json
{
    "production": { "name": "worn",  "db": "mongo.prod.internal" },
    "predev":     { "name": "dan",   "db": "mongo.dev.internal"  },
    "name":       "bob",
    "age":        33,
    "timeout":    30
}
```

| env | result |
|---|---|
| `production` | `name=worn`, `db=mongo.prod.internal`, `age=33`, `timeout=30` |
| `predev` | `name=dan`, `db=mongo.dev.internal`, `age=33`, `timeout=30` |
| `unknown` (no match, no `default` dict) | data returned unchanged |

Add a `default` dict to handle unknown envs:

```json
{
    "production": { "name": "worn" },
    "default":    { "name": "bob" },
    "age":        33
}
```

`unknown` env → `name=bob`, `age=33`.

---

### Pattern 3 — env-named top-level dicts mixed with scalar defaults

```json
{
    "prod":   { "name": "worn" },
    "predev": { "name": "dan" },
    "name":   "bob",
    "age":    33
}
```

Top-level dicts whose key matches the resolved env are applied as overrides. Top-level scalars are universal defaults merged underneath:

| env | result |
|---|---|
| `prod` | `name=worn`, `age=33` |
| `predev` | `name=dan`, `age=33` |
| `unknown` (no matching dict, no `default` dict) | data returned unchanged |

You can also include a `default` dict for unknown env fallback:

```json
{
    "prod":    { "name": "worn" },
    "default": { "name": "bob" },
    "age":     33
}
```

`unknown` env → `name=bob`, `age=33`.

---

### `default` fallback

In all three patterns, if the resolved env has no matching key, varTrack silently falls back to `default`. If there is no `default` either, the data is returned unchanged.

---

## `root_key` — extracting a subtree before slicing

Real config files often live alongside other tooling config (Spring, Maven, npm, etc.). Use `root_key` to extract only the varTrack-relevant subtree before env-detection runs.

```cue
rules: [{
  root_key: "vartrack"   // default — extract "vartrack" subtree if present
}]
```

Set `root_key: ""` to always process the whole file. If the key doesn't exist, varTrack soft-falls back to the whole file.

### Example — `package.json`

A Node.js project that also carries deploy config inside `package.json`:

```json
{
  "name": "my-app",
  "version": "1.0.0",
  "scripts": { "start": "node index.js" },
  "vartrack": {
    "production": { "API_URL": "https://api.prod.com",  "LOG_LEVEL": "warn"  },
    "staging":    { "API_URL": "https://api.stg.com",   "LOG_LEVEL": "info"  },
    "default":    { "API_URL": "https://api.local.com", "LOG_LEVEL": "debug" }
  }
}
```

```cue
rules: [{
  file_name: "package.json"
  root_key:  "vartrack"       // ignore name/version/scripts — only sync vartrack block
  branch_map: { main: "production", develop: "staging" }
}]
```

Push to `main` → `API_URL=https://api.prod.com`, `LOG_LEVEL=warn` written to the sink.

### Example — `pom.xml` (Maven)

A Java project with deploy properties in `pom.xml`. varTrack parses XML and extracts the subtree:

```xml
<project>
  <groupId>com.example</groupId>
  <artifactId>my-service</artifactId>

  <vartrack>
    <production>
      <database_host>mongo.prod.internal</database_host>
      <max_connections>100</max_connections>
    </production>
    <staging>
      <database_host>mongo.staging.internal</database_host>
      <max_connections>10</max_connections>
    </staging>
    <default>
      <max_connections>5</max_connections>
    </default>
  </vartrack>
</project>
```

```cue
rules: [{
  file_name: "pom.xml"
  root_key:  "vartrack"
  branch_map: { main: "production", develop: "staging" }
}]
```

Push to `main` → `database_host=mongo.prod.internal`, `max_connections=100`.

---

## Combining strategies

Strategies can be combined. varTrack uses the first one that matches:

```cue
rules: [{
  platform:   "github"
  datasource: "mongo"
  branch_map: {
    main:    "production"   // explicit — highest priority
    develop: "staging"
  }
  env_as_pr:     true       // fallback for PRs not in branch_map
  env_as_branch: true       // fallback for all other branches
}]
```

| Event | Resolved env |
|---|---|
| Push to `main` | `production` (from `branch_map`) |
| Push to `develop` | `staging` (from `branch_map`) |
| PR #42 opened | `pr-42` (from `env_as_pr`) |
| Push to `feature/x` | `feature/x` (from `env_as_branch`) |
