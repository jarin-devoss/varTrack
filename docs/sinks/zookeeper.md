# ZooKeeper Sink

varTrack writes config values to ZooKeeper as znodes. ZooKeeper is ideal for distributed coordination config and service discovery.

---

## Configuration

```cue
datasources: [{
  zookeeper: {
    tag:      ""
    hosts:    ["zookeeper:2181"]
    base_path: "/vartrack"       // optional root path
  }
}]
```

### With authentication

```cue
datasources: [{
  zookeeper: {
    hosts:         ["zookeeper:2181"]
    username:    "vartrack"
    password:    "secret"
    auth_scheme: "digest"    // default
  }
}]
```

---

## Destination template

The `destination_template` sets the root znode path under which config keys are written as child znodes:

```cue
rules: [{
  platform:             "github"
  datasource:           "zookeeper"
  destination_template: "/{tenant}/{env}"
}]
```

Each config key becomes a child znode:

```
/acme/production/database.host     = "mongo.prod.internal"
/acme/production/max_connections   = "50"
/acme/production/feature.dark_mode = "true"
```

A znode per key is the only shape ZooKeeper stores, so `update_strategy` is `STRATEGY_KEY_VALUE` or unset. With `STRATEGY_DOCUMENT` or `STRATEGY_FILE` the sync fails and says so.

---

## Znode encoding

```cue
datasources: [{
  zookeeper: {
    hosts:    ["zookeeper:2181"]
    encoding: "plain"    // "plain" (default), "json", "base64"
  }
}]
```

| Encoding | Stored as |
|---|---|
| `plain` | Raw UTF-8 string |
| `json` | JSON-encoded value |
| `base64` | Base64-encoded bytes — safe for binary values |

---

## Node types

```cue
datasources: [{
  zookeeper: {
    hosts:      ["zookeeper:2181"]
    ephemeral:  false    // create persistent znodes (default)
    sequential: false    // create sequential znodes
  }
}]
```

---

## ACL

```cue
datasources: [{
  zookeeper: {
    hosts:          ["zookeeper:2181"]
    acl_scheme:     "digest"
    acl_id:         "vartrack:hashedpassword"
    world_readable: true   // also grant world:anyone read permission
  }
}]
```

!!! warning "Not applied yet"
    The ZooKeeper sink does not read the ACL settings, `auth_scheme`, `ephemeral`, `sequential` or `encoding`: they are accepted by the loader and have no effect, and the orchestrator logs that once per datasource. Nodes are created with the open ACL.

---

## One write, one step

The znodes a file changes and its `__vartrack__` stamp are written in one ZooKeeper transaction: the server applies all of its operations or none. A refused transaction (no permission, a session that expired) fails the sync and leaves every znode as it was, stamp included. Missing parent znodes are created first, outside the transaction, since a transaction cannot make a path; that is harmless on a failure. A znode that another writer creates or deletes between varTrack looking and committing fails the transaction, and varTrack looks again once.

A transaction is one request, and the server refuses one over `jute.maxbuffer` (1 MiB by default). A file whose znodes do not fit is written as several transactions, each whole, with a warning in the log; the stamp goes in the last, and a failure between them leaves the earlier transactions written.

---

## Drift detection

The watcher asks the orchestrator each poll; the orchestrator reads the znodes varTrack wrote and compares them with what it last synced from Git — a changed or missing key is drift ([Drift Detection](../concepts/drift-detection.md)).

```cue
rules: [{
  platform:   "github"
  datasource: "zookeeper"
  self_heal:  true
}]
```
