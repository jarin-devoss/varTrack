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

!!! note "Orchestrator image"
    This sink runs the `helm` CLI, which the orchestrator image leaves out by default. Build it with `--build-arg HELM_VERSION=v3.16.3`.

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
    release_name:     "myapp"
    chart_name:       "./charts/myapp"
    upgrade_strategy: "INSTALL_OR_UPGRADE"
  }
}]
```

| Strategy | What it runs |
|---|---|
| `INSTALL_OR_UPGRADE` | `helm upgrade --install --create-namespace RELEASE CHART --set …` — installs the release the first time, upgrades it after |
| `FULL_UPGRADE` | `helm upgrade RELEASE CHART --set …` — chart and values; the release must already exist |
| `VALUES_ONLY` | `helm upgrade RELEASE CHART --reuse-values --set …` — sets varTrack's values on the existing release and keeps its other values; the release must already exist |
| unset | No Helm command: the values are written to a values file (`helm_values_file_path` in the rule, default `values.yaml`) for other tooling to apply |

`chart_version` adds `--version` and `chart_repo` adds `--repo`. Values go in as `--set` flags; `helm_values_mode: "file"` in the rule passes them as a values file instead.

```bash
# INSTALL_OR_UPGRADE, release myapp-production
helm upgrade --install --create-namespace myapp-production ./charts/myapp \
  --namespace default --history-max 10 \
  --set database.host=mongo.prod.internal \
  --set maxConnections=50
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

The watcher asks the orchestrator each poll; the orchestrator reads the release's current values and compares them with what it last synced from Git — a changed or missing key is drift ([Drift Detection](../concepts/drift-detection.md)).

```cue
rules: [{
  platform:   "github"
  datasource: "helm"
  self_heal:  true
}]
```
