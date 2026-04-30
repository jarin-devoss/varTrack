# Why varTrack?

## The problem

Most teams manage runtime configuration through one of two approaches — and both have serious weaknesses.

**Direct database edits**

- No audit trail — who changed `max_connections` from 50 to 5 at 3 AM?
- Changes are invisible to the rest of the team
- Drift goes undetected until something breaks
- No validation — typos and bad values go straight to production

**Scattered `.env` and config files**

- Duplicated across repos and deployment scripts
- Environments drift out of sync silently
- No single place to see what is deployed where
- Secrets baked into files or CI variables with no rotation story

---

## The Git-as-source-of-truth model

varTrack treats your Git repository as the single authority for configuration data:

- Every change is a commit — full audit trail, PR review, rollback
- Push to `main` → production synced; push to `develop` → staging synced
- Invalid configs are rejected before any write (CUE schema validation)
- Drift is caught within one poll interval and optionally healed automatically

---

## How it compares to ArgoCD and FluxCD

ArgoCD and FluxCD are the dominant GitOps tools for Kubernetes. They are excellent at what they do: reconciling **Kubernetes manifests** from a Git repo into a cluster. varTrack solves a different problem.

| | ArgoCD / FluxCD | varTrack |
|---|---|---|
| **What it syncs** | Kubernetes resources (Deployments, Services, CRDs…) | Configuration data: MongoDB, Redis, ZooKeeper, S3, ConfigMap, Helm values, Vercel env vars, Linux files |
| **Where config lives** | Kubernetes etcd | The application's own datastore |
| **Drift detection** | Kubernetes state only | Any datasource — MongoDB, Redis, ZooKeeper, S3 |
| **Secret management** | External Secrets Operator / Sealed Secrets | HashiCorp Vault with `@secret()` annotations |
| **Schema validation** | None | CUE schema per tenant, enforced before every write |
| **Non-K8s targets** | ✗ | ✓ Redis, MongoDB, ZooKeeper, S3, Linux SSH, Vercel |
| **Multi-sink fan-out** | ✗ | ✓ One push → multiple sinks in parallel |

**They are complementary, not competing.** A typical setup:

```
Git push
  ├── ArgoCD → reconciles your Kubernetes Deployment
  └── varTrack → syncs runtime config into MongoDB + Redis + ConfigMap
```

---

## How it compares to HashiCorp Consul

Consul is a service mesh and distributed KV store. It doesn't help you get config *into* Consul from Git, validate it, or fan it out to other systems.

varTrack can write to Consul (via ZooKeeper-compatible API or directly), with full Git history, schema validation, drift detection, and multi-env routing.

---

## How it compares to manual Helm values

Helm values files in Git are a common pattern, but they have limitations:

- Values are only applied on `helm upgrade` — no continuous reconciliation
- No fan-out to non-Helm sinks (MongoDB, Redis, etc.)
- No runtime drift detection — if someone `kubectl edit`s the ConfigMap, nobody knows

varTrack's Helm sink triggers `helm upgrade` on push and the watcher can detect if the deployed values diverge from what Git says.

---

## When varTrack is the right fit

✅ You store config, feature flags, or environment variables in MongoDB, Redis, S3, ZooKeeper, ConfigMaps, Helm releases, Vercel, or Linux servers — and want Git as the single authority

✅ You have multiple environments (dev, staging, production) and want changes to flow automatically on push without manual scripts

✅ You need drift detection — if someone edits a value directly in any sink, varTrack restores the correct state

✅ You want secrets to stay in Vault and never touch Git — `@secret()` annotations resolve at sync time

✅ You need to validate configs before they reach the datastore — CUE schemas catch bad values at the gate

## When varTrack is NOT the right fit

❌ You only need to sync Kubernetes manifests — ArgoCD or FluxCD is the better tool

❌ You want a general-purpose feature flag service (LaunchDarkly, Unleash) with gradual rollouts and targeting rules

❌ Your config is too dynamic to live in Git (e.g. user-generated content, real-time game state)
