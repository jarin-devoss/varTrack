# Linux Server Sink

varTrack writes config values as a file on a remote Linux server over SSH (SFTP). Useful for servers that read configuration from flat files (`.env`, `.properties`, JSON, YAML) and can't use a central datastore.

---

## Configuration

```cue
datasources: [{
  linux_server: {
    tag:      ""                         // optional — resolves to "linux_server-{tag}"
    host:     "server.example.com"
    port:     22                         // optional, default 22
    username: "deploy"

    // One of password or ssh_key:
    ssh_key: {private_key: {file: "/run/secrets/id_rsa"}}
    // password: {file: "/run/secrets/ssh-password"}

    base_directory: "/etc/app"
    file_paths:     ["app.env"]          // the file written; relative paths are under base_directory
    env_as_path:    false                // true: one directory per environment

    known_hosts_file: "/etc/vartrack/known_hosts"   // optional
  }
}]
```

The SSH key is read from a file, so give `private_key` as `{file: "/path"}`. `password` takes any credential form: a string, `{file: …}` or `{ref: …}` from Vault.

---

## Where the file goes

| Setting | `production` is written to |
|---|---|
| default | `/etc/app/app_production.env` |
| `env_as_path: true` | `/etc/app/production/app.env` |
| rule `destination_template: "/etc/app/{env}.env"` | `/etc/app/production.env` |

`destination_template` on the rule takes precedence over `base_directory` and `file_paths`. Missing directories are created.

The file's extension sets its format: `.env` writes `key=value` lines; `.json`, `.yaml`, `.properties` and `.ini` are written in their own format. Keys are written as they come out of the config file, sorted; `.env`, `.properties` and `.ini` files start with a header line:

```bash
# /etc/app/production/app.env — from a YAML file with database.host, feature.dark_mode, max_connections
# vartrack managed — env=production tenant=default
database.host=mongo.prod.internal
feature.dark_mode=true
max_connections=50
```

---

## Host keys

```cue
host_key_verification: "strict"     // "strict" (default), "accept-new", "off"
known_hosts_file:      "/etc/vartrack/known_hosts"
```

| Mode | Behaviour |
|---|---|
| `strict` | The host's key must already be in `known_hosts_file` (or the orchestrator's system known hosts) |
| `accept-new` | Needs `known_hosts_file`. A host seen for the first time is accepted and its key is written to that file before the first command runs, so the next connection checks it; a changed key is refused |
| `off` | Any key is accepted and nothing is recorded. Every connection logs a warning: a man in the middle would not be detected. Not recommended |

---

## Writing the file

```cue
linux_server: {
  // …connection, file_paths…
  file_mode:           "0640"
  file_owner:          "app:app"          // chown; usually needs use_sudo
  create_directories:  true               // default; false fails when the directory is missing
  backup_before_write: true
  backup_suffix:       ".bak"             // default
  validation_command:          "nginx -t -c {file}"
  rollback_on_validation_fail: true
  post_write_commands:         ["systemctl reload nginx"]
  continue_on_command_error:   false
}
```

In order: missing directories are created, the previous file is copied to `{file}{backup_suffix}` when `backup_before_write` is on, the new file is written, then `file_mode` and `file_owner` are applied. `validation_command` runs next — `{file}` is the written file — and when it fails the sync fails; with `rollback_on_validation_fail` the previous file is put back first. Last, `post_write_commands` run in order: a failing one fails the sync unless `continue_on_command_error` is on.

## Transfer, sudo and connection

| Field | Effect |
|---|---|
| `use_sftp` | Files move over SFTP (default); `false` moves them through shell commands, for servers without SFTP |
| `use_sudo`, `sudo_password` | File operations and commands run with `sudo`; the file is uploaded to `/tmp` first and installed from there. `sudo_password` takes any credential form |
| `connection_timeout`, `command_timeout` | Limits for connecting and for each command |
| `max_retries`, `retry_backoff` | Connection attempts after the first, and the wait between them; a changed host key is never retried |
| `enable_compression` | SSH compression |

Files are written as the SSH user (or root with `use_sudo`); without `file_mode` they get that user's default permissions.

---

## Drift detection

The watcher asks the orchestrator each poll; the orchestrator reads the remote file over SSH and compares its keys with what it last synced from Git — a changed or missing key is drift ([Drift Detection](../concepts/drift-detection.md)).

```cue
rules: [{
  platform:   "github"
  datasource: "linux_server"
  self_heal:  true
}]
```
