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
    auth_username: "vartrack"
    auth_password: "secret"
    auth_scheme:   "digest"    // default
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
    hosts: ["zookeeper:2181"]
    acl: {
      scheme:         "digest"
      id:             "vartrack:hashedpassword"
      world_readable: true   // also grant world:anyone read permission
    }
  }
}]
```

---

## Drift detection

The watcher recursively lists all znodes under the configured path and compares their data (bytes) against the Git baseline. It also registers a ZooKeeper session watcher for near-real-time detection in addition to the periodic poll.

```cue
rules: [{
  platform:   "github"
  datasource: "zookeeper"
  self_heal:  true
}]
```
