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
      "file_name":  "app-settings.yaml",
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

## 2. Declare the tenant in `config.cue`

The tenant is yours to declare, not something a request can create. Add it to `config.cue`'s
[`tenants`](../configuration/bundle.md#tenants), with the repositories whose pushes are its own
and its schema repository:

```cue
tenants: [{
  name:         "payments"
  repositories: ["payments/*"]
  schema_repo: {
    platform: "github"
    repo:     "payments/schemas"
    branch:   "main"
    token:    {ref: {path: "payments/git", key: "token"}}   // read access to the schema repository
  }
}]
```

Restart the orchestrator API and its workers. The orchestrator clones the schema repository on
first use into `SCHEMA_CACHE_DIR`; give the API and the workers the same directory (a shared
volume) so they clone once.

> Earlier releases registered a tenant with `SCHEMA_TENANT_PAYMENTS_REPO`, `_BRANCH` and
> `_TOKEN`. Those variables are still read for this release, with a warning; a tenant declared
> in `config.cue` wins over one of the same name from the environment.

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

A `config.cue` rule that no tenant's `bundle.json` names still syncs, as its first tenant's;
with none listed, as the declared tenant whose `repositories` the rule covers (`default` when
there is none).

## 4. Keep the settings fresh

A changed `bundle.json`, schema or preset is picked up when the tenant's clone is next
refreshed: every `SCHEMA_TTL_SECONDS` (300 s by default).

A push webhook (`POST /webhooks/schema-registry` on the gateway) refreshes at once the tenant
whose `schema_repo` the push came from; a push from any other repository is refused. If a refresh fails, the rules already loaded stay in use and the
orchestrator logs `schema repo of tenant payments could not be refreshed; using the rules
already loaded`.

## 5. Check what the tenant sees

In the web UI, a signed-in user sees the rules of every tenant their Git account can read;
the **Tenant** column appears when more than one tenant is visible. A rule's page shows the
rule in `config.cue`; the sync's task shows what was written.

See [Authentication & Authorization](../configuration/auth.md#web-ui-sign-in-with-your-git-platform)
for who sees what.
