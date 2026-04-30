# Helm Sink

varTrack writes config values to a Helm release. Git becomes the source of truth for Helm chart configuration.

---

## Configuration

```cue
datasources: [{
  helm: {
    tag:        ""
    namespace:  "default"
    kubeconfig: "/path/to/kubeconfig"   // optional — uses in-cluster config if absent
  }
}]
```

---

## Destination template

The `destination_template` sets the Helm release name:

```cue
rules: [{
  platform:             "github"
  datasource:           "helm"
  destination_template: "myapp-{env}"
}]
```

| Push branch | Helm release updated |
|---|---|
| `main` | `myapp-production` |
| `develop` | `myapp-staging` |

---

## Upgrade strategies

```cue
datasources: [{
  helm: {
    upgrade_strategy: "VALUES_ONLY"   // default
  }
}]
```

| Strategy | What it does |
|---|---|
| `VALUES_ONLY` | Inject new values into the existing release; chart version unchanged |
| `FULL_UPGRADE` | Run a full `helm upgrade` — chart + values |
| `INSTALL_OR_UPGRADE` | Install the release if it doesn't exist, upgrade otherwise |

### `VALUES_ONLY` (default)

Writes a `values.yaml` and applies it without changing the chart:

```yaml
# Generated values.yaml
database:
  host: mongo.prod.internal
maxConnections: 50
featureFlags:
  darkMode: true
```

### `FULL_UPGRADE`

Runs a full Helm upgrade with the new values:

```bash
helm upgrade myapp-production ./chart \
  --set database.host=mongo.prod.internal \
  --set maxConnections=50
```

### `INSTALL_OR_UPGRADE`

Equivalent to `helm upgrade --install` — safe for first-time deployments:

```bash
helm upgrade --install myapp-production ./chart \
  -f generated-values.yaml
```

---

## Chart configuration

```cue
datasources: [{
  helm: {
    chart_name:    "myapp"
    chart_version: "1.2.3"     // optional — pin chart version
    chart_repo:    "https://charts.myorg.com"
  }
}]
```

---

## Upgrade options

```cue
datasources: [{
  helm: {
    wait:            true    // wait for all resources to be ready
    wait_timeout:    "5m"   // timeout for --wait
    atomic:          true    // roll back on failure
    force:           false   // force resource updates
    cleanup_on_fail: true    // delete new resources if upgrade fails
    no_hooks:        false   // skip pre/post-upgrade hooks
    max_history:     10      // number of release versions to keep
  }
}]
```

---

## Drift detection

The watcher reads the current Helm release values and compares against the Git baseline.

```cue
rules: [{
  platform:   "github"
  datasource: "helm"
  self_heal:  true
}]
```
