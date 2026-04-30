# Sinks

A sink is a datasource that varTrack writes config values into. One push can fan out to multiple sinks simultaneously — just add multiple rules pointing at different datasources.

---

## Available sinks

| Sink | Use case |
|---|---|
| [MongoDB](mongodb.md) | Feature flags, app config, tenant settings stored as documents |
| [Redis](redis.md) | Low-latency config reads, real-time feature flags |
| [ZooKeeper](zookeeper.md) | Distributed coordination config, service discovery values |
| [S3](s3.md) | Config files as objects, audit storage, batch pipelines |
| [Kubernetes ConfigMap](configmap.md) | App config for Kubernetes workloads |
| [Helm](helm.md) | Helm values files, chart config driven by Git |
| [Vercel](vercel.md) | Frontend environment variables synced from Git |
| [Linux Server](linux-server.md) | Write config files directly to remote servers via SSH |

---

## Multi-sink fan-out

Push once, write to all of them:

```cue
rules: [
  { platform: "github", datasource: "mongo",      file_name: "config.yaml", destination_template: "{env}-config" },
  { platform: "github", datasource: "redis",      file_name: "config.yaml", destination_template: "{env}:cfg" },
  { platform: "github", datasource: "zookeeper",  file_name: "config.yaml", destination_template: "/{tenant}/{env}" },
  { platform: "github", datasource: "s3",         file_name: "config.yaml", destination_template: "{tenant}/{env}/" },
]
```

All four writes run in parallel on each push.

---

## Multiple instances of the same type

Use `tag` to run two MongoDB instances side by side:

```cue
datasources: [
  { mongo: { tag: "primary",  endpoint: "mongodb://mongo-primary:27017",  database: "app" } },
  { mongo: { tag: "replica",  endpoint: "mongodb://mongo-replica:27017",  database: "app" } },
]

rules: [
  { platform: "github", datasource: "mongo-primary", ... },
  { platform: "github", datasource: "mongo-replica",  ... },
]
```

The resolved datasource name is `mongo-{tag}` when a tag is set, or `mongo` when the tag is empty.

---

## Destination template

Every sink supports `destination_template` to control where data lands using `{env}` and `{tenant}` placeholders. See the [Destination Template](../concepts/destination-template.md) page for the full reference.
