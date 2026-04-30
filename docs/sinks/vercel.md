# Vercel Sink

varTrack writes config values to Vercel environment variables. Git becomes the source of truth for your Vercel project configuration.

---

## Configuration

```cue
datasources: [{
  vercel: {
    tag:        ""
    token:      "vercel_token_xxx"
    project_id: "prj_xxxx"
    team_id:    "team_xxxx"   // optional — for team-scoped projects
  }
}]
```

---

## Destination template

The `destination_template` sets the environment variable prefix:

```cue
rules: [{
  platform:             "github"
  datasource:           "vercel"
  destination_template: "{env}_"
}]
```

| Config key | Vercel env var |
|---|---|
| `DATABASE_HOST` | `production_DATABASE_HOST` |
| `MAX_CONNECTIONS` | `production_MAX_CONNECTIONS` |

---

## Deployment targets

Control which Vercel deployment environments receive the variables:

```cue
datasources: [{
  vercel: {
    token:      "vercel_token_xxx"
    project_id: "prj_xxxx"
    targets: ["production", "preview", "development"]  // all enabled by default
  }
}]
```

| Target | When it applies |
|---|---|
| `production` | Production deployments (`main` branch by default) |
| `preview` | Preview deployments (feature branches / PRs) |
| `development` | Local `vercel dev` environment |

---

## Variable type

```cue
datasources: [{
  vercel: {
    token:      "vercel_token_xxx"
    project_id: "prj_xxxx"
    var_type:   "plain"     // "plain" (default), "secret", "sensitive"
  }
}]
```

| Type | Description |
|---|---|
| `plain` | Readable in the Vercel dashboard |
| `secret` | Encrypted — not visible in the dashboard |
| `sensitive` | Same as secret; can't be read back via API after creation |

---

## Drift detection

The watcher reads Vercel environment variables and compares against the Git baseline.

```cue
rules: [{
  platform:   "github"
  datasource: "vercel"
  self_heal:  true
}]
```
