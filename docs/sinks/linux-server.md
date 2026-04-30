# Linux Server Sink

varTrack writes config values as a file on a remote Linux server over SSH. Useful for servers that read configuration from flat files (`.env`, INI, etc.) and can't use a central datastore.

---

## Configuration

```cue
datasources: [{
  linux_server: {
    tag:         ""
    host:        "server.example.com"
    port:        22
    username:    "deploy"
    private_key: "-----BEGIN OPENSSH PRIVATE KEY-----\n..."
    base_path:   "/etc/app"
  }
}]
```

### Password authentication

```cue
datasources: [{
  linux_server: {
    host:     "server.example.com"
    username: "deploy"
    password: "secret"
  }
}]
```

---

## Destination template

The `destination_template` sets the full remote file path:

```cue
rules: [{
  platform:             "github"
  datasource:           "linux_server"
  destination_template: "/etc/app/{env}.env"
}]
```

| Push branch | File written |
|---|---|
| `main` | `/etc/app/production.env` |
| `develop` | `/etc/app/staging.env` |

The file is written in `KEY=value` format by default:

```bash
# /etc/app/production.env
DATABASE_HOST=mongo.prod.internal
MAX_CONNECTIONS=50
FEATURE_DARK_MODE=true
```

---

## File permissions & ownership

```cue
datasources: [{
  linux_server: {
    host:              "server.example.com"
    username:          "deploy"
    private_key:       "..."
    file_permissions:  "0640"       // octal — owner rw, group r
    file_owner:        "app"        // chown user
    file_group:        "appgroup"   // chown group
    create_parent_dirs: true        // mkdir -p before writing
  }
}]
```

---

## Backup before write

```cue
datasources: [{
  linux_server: {
    host:          "server.example.com"
    username:      "deploy"
    private_key:   "..."
    backup_suffix: ".bak"   // creates /etc/app/production.env.bak before overwrite
  }
}]
```

---

## Post-write commands

Run shell commands on the server after the file is written (e.g. reload a service):

```cue
datasources: [{
  linux_server: {
    host:        "server.example.com"
    username:    "deploy"
    private_key: "..."
    post_write_commands: [
      "systemctl reload nginx",
      "systemctl restart myapp",
    ]
    continue_on_command_error: false   // abort remaining commands on first failure
  }
}]
```

Commands run in order. Set `continue_on_command_error: true` to keep running even if one fails.

---

## Sudo

```cue
datasources: [{
  linux_server: {
    host:          "server.example.com"
    username:      "deploy"
    private_key:   "..."
    use_sudo:      true
    sudo_password: "sudo-secret"            // optional — for passworded sudo
  }
}]
```

---

## SFTP mode

By default varTrack uses SCP. Switch to SFTP for environments where SCP is disabled:

```cue
datasources: [{
  linux_server: {
    host:        "server.example.com"
    username:    "deploy"
    private_key: "..."
    enable_sftp: true
  }
}]
```

---

## Host key verification

```cue
datasources: [{
  linux_server: {
    host:                    "server.example.com"
    username:                "deploy"
    private_key:             "..."
    host_key_verification:   "strict"           // "strict" (default), "accept-new", "off"
    known_hosts_file:        "~/.ssh/known_hosts"
  }
}]
```

| Mode | Behaviour |
|---|---|
| `strict` | Reject unknown hosts — recommended for production |
| `accept-new` | Accept new hosts on first connect, reject changed keys |
| `off` | Disable host key checking — not recommended |

---

## Drift detection

The watcher connects via SSH, reads the remote file, and compares its contents against the Git baseline.

```cue
rules: [{
  platform:   "github"
  datasource: "linux_server"
  self_heal:  true
}]
```
