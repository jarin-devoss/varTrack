# Redis Sink

varTrack writes config values to Redis. Three data structures are supported: **Hash** (fields under one key), **String** (one key per variable), and **JSON** (Redis Stack).

---

## Configuration

```cue
datasources: [{
  redis: {
    tag:      ""                 // optional — resolves to "redis-{tag}"
    host:     "redis"
    port:     6379
    password: "secret" // optional
    db:       0
  }
}]
```

---

## Deployment modes

```cue
datasources: [{
  redis: {
    deployment_mode: "STANDALONE"   // default
    host:            "redis"
    port:            6379
  }
}]
```

| Mode | When to use |
|---|---|
| `STANDALONE` | Single Redis instance (default) |
| `CLUSTER` | Redis Cluster — provide multiple hosts |
| `SENTINEL` | Redis Sentinel HA — provide sentinel addresses |

### Cluster

```cue
datasources: [{
  redis: {
    deployment_mode: "CLUSTER"
    hosts: ["redis-node-1:6379", "redis-node-2:6379", "redis-node-3:6379"]
  }
}]
```

### Sentinel

```cue
datasources: [{
  redis: {
    deployment_mode: "SENTINEL"
    hosts:        ["sentinel-1:26379", "sentinel-2:26379"]
    sentinel_master: "mymaster"
    password:     "secret"
  }
}]
```

---

## Data structures

```cue
datasources: [{
  redis: {
    data_structure: "HASH"    // default — all fields under one hash key
    // data_structure: "STRING"  // one key per variable
    // data_structure: "JSON"    // Redis Stack JSON (requires RedisJSON module)
  }
}]
```

### `HASH` (default)

All config keys are stored as fields of a single hash. The hash key is the `destination_template`.

```
HSET production:cfg database.host "mongo.prod.internal"
HSET production:cfg max_connections "50"
HSET production:cfg feature.dark_mode "true"
```

Reading: `redis-cli HGET production:cfg database.host`

### `STRING`

Each config key is a separate Redis key, prefixed by `destination_template`.

```
SET production:cfg:database.host "mongo.prod.internal"
SET production:cfg:max_connections "50"
```

Reading: `redis-cli GET production:cfg:database.host`

### `JSON`

Stores the entire config as a JSON document using RedisJSON. Requires the RedisJSON module.

```
JSON.SET production:cfg $ '{"database":{"host":"mongo.prod.internal"},"max_connections":"50"}'
```

Reading: `redis-cli JSON.GET production:cfg $.database.host`

---

## Destination template

The `destination_template` sets the key prefix or hash name:

```cue
rules: [{
  platform:             "github"
  datasource:           "redis"
  destination_template: "{env}:cfg"
}]
```

---

## TLS

```cue
datasources: [{
  redis: {
    host:       "redis"
    port:       6380
    enable_tls: true
    tls_ca:     "/etc/ssl/redis-ca.pem"       // optional CA cert
    tls_cert:   "/etc/ssl/redis-client.pem"   // optional mTLS cert
    tls_key:    "/etc/ssl/redis-client.key"   // optional mTLS key
  }
}]
```

---

## Drift detection

The watcher reads all keys/fields from the configured prefix and compares against the Git baseline. Any changed or missing field triggers a drift event.

```cue
rules: [{
  platform:   "github"
  datasource: "redis"
  self_heal:  true
}]
```
