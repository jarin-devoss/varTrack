# Redis Sink

varTrack writes config values to Redis. Three data structures are supported: **Hash** (fields under one key), **String** (one key per variable), and **JSON** (Redis Stack).

---

## Configuration

```cue
datasources: [{
  redis: {
    tag:      ""                 // optional — resolves to "redis-{tag}"
    hosts:    ["redis:6379"]
    password: "secret"           // optional
    database: 0
  }
}]
```

---

## Deployment modes

```cue
datasources: [{
  redis: {
    mode:  "STANDALONE"   // default
    hosts: ["redis:6379"]
  }
}]
```

!!! warning "Only standalone is implemented"
    The sink connects to the first of `hosts` as a single instance. `mode`, `sentinel_master`, the connection pool, `write_timeout`, `enable_pipeline` and the other options marked in [`redis.proto`](https://github.com/jarin-devoss/varTrack/blob/main/proto/vartrack/v1/models/datasources/redis.proto) with `unused_reason` are accepted by the loader but read by nothing yet: set in `config.cue` they have no effect, and the orchestrator logs that once per datasource.

    `connection_timeout` and `read_timeout` are read: they bound how long the sink waits to connect and for the server's answer to a command (without them a Redis that hangs leaves the worker waiting for ever).

| Mode | When to use |
|---|---|
| `STANDALONE` | Single Redis instance (default) |
| `CLUSTER` | Redis Cluster — provide multiple hosts |
| `SENTINEL` | Redis Sentinel HA — provide sentinel addresses |

### Cluster

```cue
datasources: [{
  redis: {
    mode:  "CLUSTER"
    hosts: ["redis-node-1:6379", "redis-node-2:6379", "redis-node-3:6379"]
  }
}]
```

### Sentinel

```cue
datasources: [{
  redis: {
    mode:            "SENTINEL"
    hosts:           ["sentinel-1:26379", "sentinel-2:26379"]
    sentinel_master: "mymaster"
    password:        "secret"
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

## One write, one step

The data, the `EXPIRE`s and the `__vartrack__` stamp of a file go to Redis as one `MULTI`/`EXEC`, so a connection that dies before `EXEC` leaves neither the data nor the stamp. Every reply is checked: a command Redis refuses (the key holds another type, for example) fails the sync with the count and the first errors.

`EXEC` is not a rollback. Redis runs the commands it accepts and reports the ones it does not, so a refusal can leave the accepted ones applied; the sync fails and a retry writes the same values again. Several `hosts` use the cluster client, which cannot run one `MULTI` over keys in different hash slots, so there the same commands are sent as one pipeline instead, with the same reply check. Pruning is one `HDEL` or `DEL`, which Redis runs whole.

---

## Destination template

The `destination_template` sets the key prefix or hash name:

```cue
rules: [{
  platform:             "github"
  datasource:           "redis"
  file_name:           "app-settings.yaml"
  branch_map:           {main: "production"}
  destination_template: "{env}:cfg"
}]
```

---

## TLS

```cue
datasources: [{
  redis: {
    hosts:           ["redis:6380"]
    enable_tls:      true
    tls_ca_cert:     "/etc/ssl/redis-ca.pem"       // optional CA cert (a path)
    tls_client_cert: "/etc/ssl/redis-client.pem"   // optional mTLS cert (a path)
    tls_client_key:  "/etc/ssl/redis-client.key"   // optional mTLS key (a path)
  }
}]
```

---

## Drift detection

The watcher asks the orchestrator each poll; the orchestrator reads the fields or keys varTrack wrote and compares them with what it last synced from Git — a changed or missing key is drift ([Drift Detection](../concepts/drift-detection.md)).

```cue
rules: [{
  platform:   "github"
  datasource: "redis"
  file_name: "app-settings.yaml"
  branch_map: {main: "production"}
  self_heal:  true
}]
```
