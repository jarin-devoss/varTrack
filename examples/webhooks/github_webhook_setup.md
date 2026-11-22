# GitHub webhook setup

## Add the webhook

In the repository (or the organisation, to cover every repository at once): **Settings → Webhooks → Add webhook**.

| Field | Value |
|---|---|
| Payload URL | `https://<gateway>/webhooks/<datasource>`, e.g. `https://vartrack.example.com/webhooks/mongo` |
| Content type | `application/json` |
| Secret | the GitHub platform's `secret` in `config.cue` |
| Events | **Pushes**, and **Pull requests** when the rule sets `pr_as_env` |

The datasource in the URL is the rule's `datasource`: its type (`mongo`), or its type and tag (`mongo-primary`).

## One webhook per datasource

A rule is named by its platform and its datasource, so a push that should reach two datasources needs two webhooks:

- `https://<gateway>/webhooks/mongo-primary`
- `https://<gateway>/webhooks/mongo-dr`

GitHub sends both deliveries on each push, and each is synced on its own.

## The secret

The secret is set in two places, and nowhere else: in the webhook, and as the platform's `secret` in `config.cue`
(a string, or better a mounted file):

```cue
platforms: [{github: {endpoint: "https://github.com", secret: {file: "/run/secrets/github-webhook-secret"}}}]
```

With `APP_ENV=production` the gateway checks the `X-Hub-Signature-256` header of every delivery against it and
answers `401` when it does not match; a delivery id (`X-GitHub-Delivery`) it has seen before is refused too. Outside
production it checks neither, so run production with `APP_ENV=production`. Two GitHub platforms in one bundle each
need their own secret: the one that signed a delivery says which platform sent it.

## Which pushes are synced

The rule decides, in `config.cue`:

- `repositories` (and `exclude_repositories`) say which repositories' pushes it syncs.
- `file_name` or `file_path_map` say which files it reads, at the pushed commit.
- `branch_map` (or `branch_as_env`, `pr_as_env`, `tag_as_env`) says which environment a push feeds:

```cue
branch_map: {main: "production", develop: "staging"}
```

A push to a branch nothing maps is skipped, and its task says so (`branch feature/x feeds no environment of …`).

## Check a delivery

GitHub's **Recent Deliveries** shows the gateway's answer: `202` (queued; the body has the `task_id`), `200 event
ignored` (not a push, e.g. the first `ping`), `401` (signature), `404` (no rule for that datasource). The web UI's
rule page, or `vt task get <task_id>`, shows what the sync did. [`curl_examples.sh`](curl_examples.sh) sends signed
deliveries by hand. For Gitea, see the [Gitea example](../../docs/examples/gitea.md).
