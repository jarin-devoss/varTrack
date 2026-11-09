# NetworkPolicy manifests

Authentication is the control that protects the orchestrator
([Internal authentication](../../docs/operations/internal-auth.md)); the network is the
second one. These manifests say who may reach whom, and deny the rest.

| Policy | What it allows |
|---|---|
| [`00-default-deny.yaml`](00-default-deny.yaml) | nothing: every varTrack pod starts with no ingress |
| [`orchestrator.yaml`](orchestrator.yaml) | HTTP `8000` and gRPC `50051` from the gateway, the watcher and the web UI; admin `9093` from Prometheus only |
| [`gateway.yaml`](gateway.yaml) | webhooks and the CLI API on `5657` from anywhere (that is its job: Git platforms and `vt` users reach it); the permission engine on `5658` from the orchestrator only; admin `9092` from Prometheus only |
| [`watcher.yaml`](watcher.yaml) | admin `9091` from Prometheus only; the watcher takes no other inbound traffic |

The manifests select pods by these labels, so put them on your own Deployments (the
pod template, not just the Deployment):

| Label | Value |
|---|---|
| `app.kubernetes.io/part-of` | `vartrack` on every varTrack pod |
| `app.kubernetes.io/component` | `gateway`, `orchestrator`, `watcher` or `ui` |

Prometheus is matched by `app.kubernetes.io/name: prometheus` in a namespace named
`monitoring`; change `prometheusSelector` in each file if yours differs. The orchestrator's
workers (Celery) take no inbound traffic and so need no allow rule.

```bash
kubectl apply -n <namespace> -f deploy/networkpolicy/
```

A `NetworkPolicy` only does something when the cluster's network plugin enforces it
(Calico, Cilium and most managed offerings do; a plain `kindnet` does not). Check with a
throwaway pod that a connection to `orchestrator:50051` from outside the allowed pods is
refused after you apply them.
