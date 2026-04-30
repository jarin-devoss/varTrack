# Sync Strategies

A sync strategy controls how varTrack applies the flattened config values to a datasource. The right strategy depends on the size of your config and how often you need reads vs. writes.

---

## Strategies

=== "AUTO (recommended)"

    varTrack picks the strategy automatically based on payload size:

    - ≤ 500 keys → `GIT_UPSERT_ALL`
    - > 500 keys → `GIT_SMART_REPAIR`

    This is the default and works well for most configurations. No tuning needed.

    ```cue
    sync_mode: "AUTO"
    ```

=== "GIT_UPSERT_ALL"

    Writes every key from the flattened Git payload to the datasource in one bulk operation. Does not read the current state first.

    **Best for:** Small configs (< 500 keys), initial bootstrapping, or when you always want a clean overwrite.

    ```cue
    sync_mode: "SYNC_MODE_GIT_UPSERT_ALL"
    ```

    | | |
    |---|---|
    | Read before write | No |
    | Keys written | All |
    | Stale key cleanup | Only if `prune: true` set |
    | Best for | Small configs |

=== "GIT_SMART_REPAIR"

    Reads the current live state first, computes a diff (added / changed / removed keys), and writes only the differences. Uses BLAKE3 content hashing to detect unchanged values.

    **Best for:** Large configs (> 500 keys), or datasources where minimizing write operations matters.

    ```cue
    sync_mode: "SYNC_MODE_GIT_SMART_REPAIR"
    ```

    | | |
    |---|---|
    | Read before write | Yes |
    | Keys written | Changed only |
    | Stale key cleanup | Yes, on diff |
    | Best for | Large configs |

=== "LIVE_STATE"

    Replaces the entire stored state with Git's current view. No diff computed — direct overwrite.

    **Best for:** Low-latency sinks, or scenarios where the simplest possible write path matters more than efficiency.

    ```cue
    sync_mode: "SYNC_MODE_LIVE_STATE"
    ```

---

## Setting the strategy

Override per rule in `config.cue`:

```cue
rules: [{
  platform:   "github"
  datasource: "mongo"
  sync_mode:  "SYNC_MODE_GIT_SMART_REPAIR"
}]
```

Or let `AUTO` decide — just omit `sync_mode` or set it to `"AUTO"`.

---

## Stale key pruning

When keys are removed from your Git config file, varTrack can automatically delete them from the datasource:

```cue
rules: [{
  platform:   "github"
  datasource: "mongo"
  sync_mode:  "AUTO"
  prune:      true     // delete keys present in datasource but missing from Git
}]
```

!!! warning
    `prune: true` permanently deletes keys from the datasource that are no longer in Git. Use `prune_dry_run` or the full dry-run first to verify what would be removed.

### `prune_last`

When multiple files map to the same datasource, deletions are deferred until all files have been written. This prevents a key from being pruned because it was removed from one file but still exists in another:

```cue
rules: [{
  prune:      true
  prune_last: true   // safe for multi-file rules
}]
```

### `prune_protection`

Glob patterns for keys that must **never** be deleted, even if they're absent from Git:

```cue
rules: [{
  prune:            true
  prune_protection: ["SYSTEM_*", "_vt_*", "readonly.*"]
}]
```

---

## Dry-run

### Full dry-run — simulate the entire sync

No writes, no deletes. Returns a full report of what would be written, changed, and deleted — including which keys would be pruned.

```bash
# Via CLI
vt sync --file configs/app.yaml --datasource mongo --env staging --dry-run

# Via HTTP
POST /v1/webhooks/mongo/dry-run
```

`@secret`-annotated fields are fetched from Vault but masked as `***` in the report.

### Prune dry-run — simulate deletes only

Test the prune step independently without touching any writes. Enabled per rule:

```cue
rules: [{
  prune:         true
  dry_run_prune: true   // always simulate deletes — never actually remove keys
}]
```

With `dry_run_prune: true`, every sync runs normally (writes go through) but the delete step is always simulated and logged, never executed. Useful for auditing which stale keys would be removed before committing to live pruning.
