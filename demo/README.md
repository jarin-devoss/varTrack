# The varTrack demo

A varTrack you can push to. One command starts the whole product: a Git server (Gitea), MongoDB, ZooKeeper, Redis, S3,
Vault, an SSH target, the gateway, the orchestrator, the watcher and the web UI. A `demo-seed` container then seeds it once:
a config repository with one file, `app-settings.yaml`, three rules that read it, and a first sync of each.

It runs no test. The regression and the browser suite live in [`../e2e`](../e2e) and run on this same stack.

## Requirements

- **Docker with Compose 2.20.0 or newer** (`docker compose version`). On Linux, install Docker's own packages or Docker Desktop for
  Linux: a distribution's `docker-compose` package is often the old Compose V1, which does not run this demo (`./run.sh` says so
  and stops). On Windows and macOS, Docker Desktop.
- **About 2 GiB of memory** for the containers (1.2 GiB measured with every service up), more while the images build.
- **These ports free on the host:** 8080 (the web UI), 3000 (Gitea), 8000, 9090 and 9093 (the orchestrator), 5657 and 9092 (the
  gateway), 9091 (the watcher), 8200 (Vault), 9001 and 9002 (MinIO), 2222 (the SSH target). One that is taken stops that service from
  starting.
- **Nothing else to set up.** The demo writes only to Docker volumes, so nothing is left on your disk that only root can remove. The
  files it reads from this folder are mounted read-only with SELinux labels (`:ro,z`), so a host that enforces SELinux (Fedora, RHEL)
  can read them; that was run on Docker Desktop and on Ubuntu in CI, not on an SELinux host.

## Run it

```sh
cd demo
docker compose up        # or ./run.sh
```

The first start builds the images (2 to 4 minutes). It is ready when `demo-seed` exits and prints where to go.

**On a server, from your own machine.** Signing in uses `localhost` addresses (the UI at 8080, Gitea at 3000), so a browser on another
machine would be sent to itself. Forward the two ports and open the demo as if it ran on your own machine:

```sh
ssh -L 8080:localhost:8080 -L 3000:localhost:3000 you@the-server
```

| | |
|---|---|
| Web UI | <http://localhost:8080>: choose *Sign in with Gitea* |
| Gitea | <http://localhost:3000>: the config repository is `vartrack-demo/demo-configs` |

Four accounts exist, all with the password `VarTrack123`:

| Account | What they are |
|---|---|
| `demo-admin` | a varTrack administrator: sees and syncs every rule, and owns the repositories |
| `demo-writer` | can push to the config repository, except to its protected `main` branch |
| `demo-viewer` | can read the config repository: sees the rules, can dry-run, cannot sync |
| `demo-outsider` | can read none of it (the config repository is private): sees no rule |

The three rules, all reading `app-settings.yaml` on `main` into an environment called `default`:

| Rule | Writes to | Self-heal |
|---|---|---|
| `gitea/mongo` | MongoDB, collection `variables`, one document for the file | on |
| `gitea/zookeeper` | ZooKeeper, one znode per key under `/demo/default` | on |
| `gitea/redis-hash` | Redis database 2, the hash `demo:redis-hash:default:vartrack` | off |

The watcher looks every 10 seconds and the UI re-reads a store after 2, so everything below shows within seconds.

## The tour

Eight short things to do. Each is something you do, then what you should see. The commands are for a shell in this folder
(`demo/`); a test runs every one of them on a fresh demo, so what is written here is what happens.

### 1. Sign in and look

Open <http://localhost:8080> and sign in with Gitea as `demo-admin` (Gitea asks for consent the first time). The *Rules* page
lists `gitea/mongo`, `gitea/zookeeper` and `gitea/redis-hash`, each with its `default` environment in sync with Git.
Open one: *Environments* shows the desired state in Git next to the current state in the store, and *Keys* lists every key.

### 2. Change Git, and watch it sync

Open <http://localhost:3000/vartrack-demo/demo-configs/_edit/main/app-settings.yaml>, change `ttl_seconds` under `cache` from
`"300"` to `"310"`, and commit. The push reaches varTrack through a webhook the seed created. On a rule's *Events* tab you see
*Push received* then *Synced*, and its *Keys* tab shows `cache.ttl_seconds` as `310`.

The same change from a shell, if you prefer:

```sh tour
API=http://localhost:3000/api/v1/repos/vartrack-demo/demo-configs/contents/app-settings.yaml
FILE=$(curl -s -u demo-admin:VarTrack123 "$API")
SHA=$(echo "$FILE" | sed -n 's/.*"sha":"\([^"]*\)".*/\1/p' | head -1)
NEW=$(echo "$FILE" | sed -n 's/.*"content":"\([^"]*\)".*/\1/p' | base64 -d | sed 's/ttl_seconds: "300"/ttl_seconds: "310"/' | base64 | tr -d '\n')
curl -s -u demo-admin:VarTrack123 -X PUT -H 'Content-Type: application/json' \
  -d "{\"message\":\"cache ttl 310\",\"content\":\"$NEW\",\"sha\":\"$SHA\"}" "$API" > /dev/null
```

See it in the stores themselves:

```sh tour
docker compose exec -T redis redis-cli -n 2 HGET demo:redis-hash:default:vartrack cache.ttl_seconds
docker compose exec -T mongo mongosh vartrack --quiet --eval 'db.variables.findOne({_vt_env: "default"}).data.cache.ttl_seconds'
docker compose exec -T zookeeper zkCli.sh -server localhost:2181 get /demo/default/cache/ttl_seconds
```

### 3. Drift, healed

Change the value in MongoDB behind varTrack's back, the way an incident edit or a script would:

```sh tour
docker compose exec -T mongo mongosh vartrack --quiet --eval 'db.variables.updateOne({_vt_env: "default", _vt_file: "app-settings.yaml"}, {$set: {"data.cache.ttl_seconds": "9999"}})'
```

`gitea/mongo` has self-heal on. Within the watcher's poll a sync you did not start runs (the *Tasks* page lists it as a
self-heal), the rule's *Events* tab shows *Healed*, and `ttl_seconds` is `310` again. Nobody pressed anything. If you had the
rule's page open while the value was wrong, you also saw *Drift detected*: that event is written when a page looks at the
environment while it is drifted, so a heal that finishes between two looks leaves only *Healed*.

### 4. Drift, not healed

`gitea/redis-hash` has self-heal off. Change its value the same way:

```sh tour
docker compose exec -T redis redis-cli -n 2 HSET demo:redis-hash:default:vartrack cache.ttl_seconds 9999
```

Its `default` environment now reads *Out of sync*, and its *Diff* shows `cache.ttl_seconds` as `9999` in the store against
`310` in Git. Nothing puts it right by itself: press *Sync default* and Git's value is written back.

### 5. Dry run

Make the drift again:

```sh tour
docker compose exec -T redis redis-cli -n 2 HSET demo:redis-hash:default:vartrack cache.ttl_seconds 9999
```

Press *Dry run default* on `gitea/redis-hash`. It lists what a sync would change (`cache.ttl_seconds`, `9999` to `310`) and
writes nothing: the store still holds `9999`. Press *Sync default* to put it right.

### 6. Prune

Remove the `debug_mode` line under `features` from `app-settings.yaml` (in the web editor, or here):

```sh tour
API=http://localhost:3000/api/v1/repos/vartrack-demo/demo-configs/contents/app-settings.yaml
FILE=$(curl -s -u demo-admin:VarTrack123 "$API")
SHA=$(echo "$FILE" | sed -n 's/.*"sha":"\([^"]*\)".*/\1/p' | head -1)
NEW=$(echo "$FILE" | sed -n 's/.*"content":"\([^"]*\)".*/\1/p' | base64 -d | sed '/debug_mode/d' | base64 | tr -d '\n')
curl -s -u demo-admin:VarTrack123 -X PUT -H 'Content-Type: application/json' \
  -d "{\"message\":\"drop debug_mode\",\"content\":\"$NEW\",\"sha\":\"$SHA\"}" "$API" > /dev/null
```

The rules have `prune` on, so a key Git no longer holds is deleted from the store. `gitea/redis-hash`'s *Events* tab shows
*Keys pruned*, and the field is gone from the hash.

### 7. As someone else

Sign out and sign in as `demo-viewer`: the rules are there, *Dry run* works, and *Sync* is disabled with the reason beside it.
Sign in as `demo-outsider`: the *Rules* page is empty, because that account can read none of the config repository.

### 8. Start over

```sh
docker compose down -v
```

`docker compose down` alone stops the demo and keeps its data: the next start finds it seeded and leaves what you pushed.
`down -v` removes the data too, and the next start seeds again from nothing (`./run.sh reset` does the same).

## More

- **More sinks, two Git servers, Kubernetes.** `docker-compose.live.yml` adds S3 on MinIO, ConfigMaps and Helm releases on k3s, a
  second Gitea with a payments team, and a seeder that fills every sink. See
  [Getting started](../docs/getting-started.md#with-more-sinks-and-the-web-ui).
- **Traces, metrics, logs.** `docker compose -f docker-compose.yml -f docker-compose.observability.yml up` adds Jaeger,
  Prometheus, Grafana, Elasticsearch and Kibana.
- **The tests.** `../e2e` includes this stack and adds the regression's runner; `../ui/e2e` is the browser suite. The tour above
  is run by `ui/e2e/tour/demo-tour.spec.ts`, which reads this file.
