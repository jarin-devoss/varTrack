# Add a tenant

A tenant is a team with its own layer of rule settings, its own presets and its own CUE
schemas, kept in a **schema repository** it owns. The team running varTrack keeps
`config.cue`; the tenant changes its settings by pushing to its schema repository, without
asking anyone.

This guide adds tenant `payments`.

---

## 1. Create the schema repository

```
payments/schemas/
├── bundle.json           ← the tenant's settings for the rules in config.cue
├── app.yaml.cue          ← CUE schema for files named app.yaml
└── default.cue           ← schema for files with no schema of their own
```

`bundle.json` holds only what the tenant adds to, or changes in, a `config.cue` rule, named
by the rule's platform and datasource:

```json
{
  "extends": ["local>payments/schemas//presets/team"],
  "rules": [
    {
      "platform":   "github",
      "datasource": "mongo",
      "branch_map": {"develop": "staging"},
      "strict_validation": true
    }
  ]
}
```

Options only `config.cue` sets — `repositories`, `self_heal`, `secret_manager`, connection
settings, … — are dropped from `bundle.json` with a warning; [Rule options](../configuration/rule-options.md)
lists which option the tenant may set. A `bundle.json` that does not parse, or names a preset
that cannot be read, leaves the tenant with **no rules** until it is fixed, so check it first:

```bash
vt validate-config bundle.json --tenant payments
```

The schema files and `bundle.json` formats are in [Schema registry](../configuration/schema-registry.md).

## 2. Register the tenant with the orchestrator

Set these on the orchestrator API **and** every Celery worker, then restart them:

```bash
SCHEMA_TENANT_PAYMENTS_REPO=https://github.com/payments/schemas.git
SCHEMA_TENANT_PAYMENTS_BRANCH=main          # default: main
SCHEMA_TENANT_PAYMENTS_TOKEN=ghp_xxx        # read access to the schema repository
```

The tenant ID is the part of the name between `SCHEMA_TENANT_` and the field, lower-cased:
`PAYMENTS` → `payments`. The orchestrator clones the repository on first use into
`SCHEMA_CACHE_DIR`; give the API and the workers the same directory (a shared volume) so they
clone once.

## 3. Let the tenant layer its rules

In `config.cue`, a rule's `tenants` lists the tenants whose `bundle.json` may layer it; empty
means any tenant:

```cue
rules: [{
  platform:     "github"
  datasource:   "mongo"
  repositories: ["payments/*"]
  file_name:    "configs/app.yaml"
  branch_map:   {main: "production"}
  tenants:      ["payments"]
}]
```

A `config.cue` rule that no tenant's `bundle.json` names still syncs, as its first tenant's
(`default` when `tenants` is empty).

## 4. Keep the settings fresh

A changed `bundle.json`, schema or preset is picked up when the tenant's clone is next
refreshed: every `SCHEMA_TTL_SECONDS` (300 s by default).

A push webhook refreshes at once, but only for the schema repository named in `config.cue`'s
`schema_registry` block (`POST /webhooks/schema-registry` on the gateway); other tenants'
repositories follow the TTL. If a refresh fails, the rules already loaded stay in use and the
orchestrator logs `schema repo of tenant payments could not be refreshed; using the rules
already loaded`.

## 5. Check what the tenant sees

In the web UI, a signed-in user sees the rules of every tenant their Git account can read;
the **Tenant** column appears when more than one tenant is visible. A rule's page shows the
rule in `config.cue`; the sync's task shows what was written.

See [Authentication & Authorization](../configuration/auth.md#web-ui-sign-in-with-your-git-platform)
for who sees what.
