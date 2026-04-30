# Kubernetes ConfigMap Sink

varTrack writes config values directly into a Kubernetes ConfigMap. Changes pushed to Git are reflected in the cluster automatically — no manual `kubectl apply` needed.

---

## Configuration

```cue
datasources: [{
  configmap: {
    tag:        ""
    namespace:  "default"
    kubeconfig: "/path/to/kubeconfig"    // optional — uses in-cluster config if omitted
  }
}]
```

---

## Destination template

The `destination_template` controls the ConfigMap name:

```cue
rules: [{
  platform:             "github"
  datasource:           "configmap"
  destination_template: "myapp-{env}"
}]
```

| Push branch | ConfigMap created/updated |
|---|---|
| `main` | `myapp-production` |
| `develop` | `myapp-staging` |

Config keys are stored as ConfigMap data entries:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: myapp-production
  namespace: default
data:
  database.host: "mongo.prod.internal"
  max_connections: "50"
  feature.dark_mode: "true"
```

---

## Drift detection

The watcher reads the ConfigMap data and compares against the Git baseline. Any changed or missing key triggers a drift event.

```cue
rules: [{
  platform:   "github"
  datasource: "configmap"
  self_heal:  true
}]
```
