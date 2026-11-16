# UI end-to-end tests

These tests open the web UI in a browser against the real stack (Gitea, the gateway, the orchestrator and its worker, the
watcher, and the sinks) and use it the way people do: they sign in through Gitea's own page, click, type, read what the
page says, and then check the sinks and the API to see that what the page said is true.

They exist because a unit test of a component cannot see that the page tells the truth about the system behind it. The first
bug they were written for — a *Sync* that said "can't tell which branch feeds main" — lived between the UI, the orchestrator
and the gateway's permission engine, and no test of any one of them could fail.

## Run them

The way CI runs them, in one command from `ui/` (the demo tour on a demo of its own, then clean volumes, the 25-suite regression, the browser tests, the coverage check):

```sh
npm ci
npm run test:e2e:install      # once: the browser
npm run test:e2e:stack        # everything; --no-up for a stack that is already up, --keep to leave it running, --no-tour to skip the tour
```

Against a stack you already have (`docker compose up` in `e2e/`), with the UI image on :8080 — **not the Vite dev server**, whose
proxy is not what ships and sometimes stalls a response on Windows:

```sh
VT_UI_URL=http://localhost:8080 npm run test:e2e
VT_UI_URL=http://localhost:8080 npm run test:e2e -- e2e/specs/11-prune.spec.ts    # one spec
VT_UI_URL=http://localhost:8080 npm run test:e2e:ui                               # Playwright's UI mode: watch, step, time-travel
```

Without `VT_UI_URL` the Vite dev server is started on `:8080` (the address the demo registers for sign-in), and the config says so.
`npm run typecheck:e2e` checks the specs' types; `npm test` runs the unit tests **and the coverage manifest** (below), with no stack.

`--no-up` first runs `node e2e/stale-images.mjs` (from the repository root), which compares each running image's build time with the
last change to its sources and stops the run, naming the image, when one is behind: an old orchestrator under a new UI shows a
blank page, not an error. `VT_ALLOW_STALE_IMAGES=1` runs anyway (for the mounted stack below).

### The demo tour

`demo/README.md` has a tour of the demo (change Git, drift healed and not healed, a dry run, a prune, the viewer and the outsider).
`e2e/tour/demo-tour.spec.ts` does it: it reads that README, finds each step by its heading, runs the commands in the step's
`sh tour` blocks from `demo/`, and checks in the UI what the step says to look for. A README step with no check here, or a check
with no step, fails, so the tour cannot go out of date without CI failing. It has its own config, `playwright.tour.config.ts`, with
no world and no retries: the tour is about the demo as it is seeded, so it needs a **freshly seeded demo with its webhooks on**
(`cd demo && docker compose up -d && docker compose wait demo-seed`), not the regression's stack.

```sh
VT_UI_URL=http://localhost:8080 npm run test:e2e:tour      # VT_TOUR_PROJECT names the Compose project (default vartrack-demo)
```

## Run the stack on a machine that cannot build the images

CI builds every image from the commit it tests. An image build downloads over TLS (`buf` plugins, Go modules, pip, npm), so on a
machine where an antivirus or proxy re-signs HTTPS it stops with `x509: certificate signed by unknown authority`. Check with
`openssl s_client -connect buf.build:443 </dev/null | grep "i:"`: an issuer that is not a public CA is the cause. The fix is to
exclude the build hosts from the scan (`buf.build`, `proxy.golang.org`, `sum.golang.org`, `pypi.org`, `files.pythonhosted.org`,
`registry.npmjs.org`, `deb.debian.org`, `dl-cdn.alpinelinux.org`, `get.helm.sh`, `ghcr.io`), after which `docker compose up --build`
works as in CI. Where that is not possible, run today's code inside the images that already exist. The host trusts the scanner, so
its tools work:

```sh
buf generate                                                    # repository root: the orchestrator's Python stubs
(cd libs/go && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o /tmp/vt-stack/vartrack-bundle ./cmd/vartrack-bundle)
(cd ui && npm ci && npm run build)                              # ui/dist
```

Then a compose override kept outside the repository (say `/tmp/vt-stack/override.yml`), which mounts those over the images:

```yaml
services:
  orchestrator-api: &orchestrator
    volumes:
      - /path/to/varTrack/orchestrator-service/app:/app/app:ro
      - /tmp/vt-stack/vartrack-bundle:/usr/local/bin/vartrack-bundle:ro
  orchestrator-worker: *orchestrator
  ui:
    volumes:
      - /path/to/varTrack/ui/dist:/usr/share/nginx/html:ro
```

```sh
cd e2e
F="-f docker-compose.yml -f /tmp/vt-stack/override.yml"
docker compose $F up -d --no-build
docker compose $F wait e2e-regression                           # the 25-suite regression
cd ../ui && VT_ALLOW_STALE_IMAGES=1 VT_UI_URL=http://localhost:8080 npm run test:e2e:stack -- --no-up
```

The UI is then at http://localhost:8080 (sign in with Gitea as `demo-admin`, password `VarTrack123`). Use this stack, not the live
overlay: the overlay's ConfigMap and Helm sinks need the Kubernetes client and Helm built into the orchestrator image, which a
mount cannot add. After changing Python or the UI, rebuild `ui/dist` as above and restart `orchestrator-api` and
`orchestrator-worker`; after rebuilding a mounted Go binary, recreate the service (`up -d --no-deps --force-recreate`), because a
restart keeps the old file's pages and the process crashes.

## How a run goes

1. `global-setup.ts` signs in once per role (admin, writer, reader, outsider) through the real "Sign in with Gitea" and
   Gitea's consent page, and saves each browser state to `e2e/.auth/<role>.json`.
2. `fixtures/world.ts` builds what the specs expect to find, the same whatever an earlier run left behind: a `develop` branch
   next to `main`, team folders, the schema repository with the CUE schemas and `world/bundle.json` (a rule for each kind of
   environment: `branch_map`, `branch_as_env`, `file_path_map`, with and without self-heal and
   strict validation), and the first syncs, pushed as signed webhooks. Every rule names its file and its environments (one that does not is refused).
   Last, it forgets every environment of those rules that `WORLD.environments` does not name (an earlier run's leftovers, and what
   the regression suites wrote, such as `default` in `redis-string`, which a `branch_as_env` rule counts as fed) and logs which, then
   checks that each rule has exactly the environments the world names: a world that is not what it says fails setup with one line
   naming the rule, not a spec somewhere downstream. A spec about leftovers makes its own (`leaveBehind`) and forgets it after.
3. Specs run **one after the other** (`workers: 1`): they share the stack. **No spec relies on another**: each makes what it reads
   (`made`, `tasks`, `stack`), puts back what it changes, and passes alone and in any order.

## Ask, do not wait

A long wait means the product is missing a way to say "now". Do not raise a timeout; use the way to ask:

- **The watcher:** `stack.checkNow('gitea', 'mongo')` makes it look at that rule and heal it now, and says what it found. The demo's
  watcher also polls every 10 s, but a spec never depends on that.
- **The page's own Refresh** reads the sinks now (`GET …?fresh=true`; `api.fresh(rule)` in a spec), not the last answer.
- **A sink that is down** fails within its `connect_timeout_ms` (5 s in the demo), so "Unknown" shows in seconds.
- **A task:** `api.succeeded(id)` waits for it and fails at once, with its message, if it fails; `api.finished(id)` when failing is the point.
- **A task a spec only reads:** `made.ok()`, `made.dry()`, `made.failed()` make it the first time and share it for the run.

## Write a spec

- **Say what a person does and sees.** `test('a sync of production asks for the word "prune" first, and deletes nothing until it
  is typed', …)`. Then check the effect where it happens: the sink (`stores`), Git (`gitea`), the API (`api`).
- **Sign in as the role the spec is about**: `test.use(as('reader'))`. A spec about what the roles may do runs for each of them.
- **Find things as a person would**: `getByRole`, `getByLabel`, `getByText`, accessible names. A control a screen reader cannot
  name is a bug in the UI: give it an `aria-label`, do not reach for a CSS class. (Structural classes such as `.rowx`, `.pill`,
  `.hero` are used only to scope a search inside a row or card.)
- **One page object per screen** in `pages/`, with getters (not field initialisers), actions, and a `ready()` that waits for the
  page to have loaded. A spec reads like the steps; selectors live in the page object.
- **Switch a tab with `showTab`, never with a bare click.** `showTab(tabs, name, ready?)` (`pages/tabs.ts`, wrapped by
  `RulePage.showKeys()`, `showRuleTab()`) clicks once and waits for the tab to be the selected one, then for its content, and
  fails naming the tab if it is not: a click that has not landed is not a state to go on from. A spec about what a tab shows
  opens it by address (`RulePage.openKeys()`, `?tab=keys`) and leaves the click to one spec about the tab. Name a tab or an
  option `exact`, or `staging` also matches `Diff staging`, `Sync staging`, …
- **Name the environment, rule and tab a spec acts on.** Do not rely on what a page shows first (the Keys tab opens on the
  first environment that differs, which is not always the one you mean) or on what an earlier spec or the regression left. A spec
  needs only the world (`fixtures/world.ts`): pick the environment (`RulePage.keysEnvironment(env)`) and wait for it.
- **No `test.skip`, and no branch on what the stack holds.** Make the state the test is about (a drift, a failed sync) and put it
  back after (`withDrift` in `04-home.spec.ts`). A skipped test covers nothing, so the coverage check fails the run on one, and
  the job summary lists it next to the flaky tests.
- **A spec you add or change is run again, with no retry, before it is trusted.** On a pull request CI runs the spec files the
  change touches `BURN_IN_REPEAT` (5) times each with `--retries=0`: the one retry on CI is what lets a flaky test pass. It sees
  spec files only, not the page objects they import (that would be most of the suite), so a change to a page object names the
  specs it is for and runs them yourself first:
  `npx playwright test e2e/specs/07-rule-detail.spec.ts --repeat-each=20 --retries=0` against a stack.
- **Never sleep.** Assertions retry (`expect(locator).toBeVisible()`), and `expect.poll` waits on the API or a sink. A
  `waitForTimeout` is allowed only to show that something did *not* happen, and says how long and why.
- **Make your own data and clean up after it**: `unique('name')` for every value you push; restore a file, a key, a service in
  `finally` or `afterEach`. The `faults` fixture starts a stopped service again by itself; the `stores` helpers change a sink
  directly. A spec that leaves the world changed breaks every spec after it.
- **Read before you assert.** Open the component, find the role and accessible name, or read the page snapshot in
  `test-results/*/error-context.md` after a failure. Write the locator from that, not from memory of the page.
- **Edit files with the Write/Edit tools**, not with string replacement in a shell: it turns `
` into a line break and drops braces.
- **Probes are scratch specs, deleted after**, and every change they make to a store sits in `try/finally`.
- **A skip says why**: `test.skip(condition, 'reason')`. A bare skip fails the coverage check.
- **A controlled input's state lives in the URL or a store** and changes after the router does: use `click()` and a retrying
  assertion, not `check()`/`uncheck()`.
- **Do not wait for `networkidle`**: the pages poll, so the network never goes quiet. Wait for the page's own loading rows to be
  gone.

## Fixtures

| Fixture | What it is |
|---|---|
| `page`, `context` | the browser, signed in as the role of the spec |
| `api` | the orchestrator's UI API as that person; `adminApi` as the admin |
| `stack` | calls only the stack's own services make: a signed push webhook, a schema warm-up |
| `gitea` | the Git server as its admin: read and push files, branches, make users and tokens |
| `stores` | read and change Redis and MongoDB directly, as someone with access to the store |
| `faults` | stop, pause and start a service of the stack |

## The coverage manifest

`coverage.json` lists every route in `src/App.tsx`, every literal `aria-label` in `src/`, and every status the UI words
(`SYNC`, `DRIFT`, `TASK`, `FILE`, `TRIGGER` in `src/lib/status.ts`, `REASON` in `src/features/events.tsx`), each with the spec
that covers it — or, where nothing can, the reason, and what holds it instead.

`npm test` (`src/lib/coverage.test.ts`) fails when:

- the source has a route, labelled control or status the manifest does not list, or the manifest lists one the source no longer
  has;
- an entry names a spec that does not exist, or one whose text (and page objects) never mention it;
- a spec skips a test without a reason.

CI then runs the browser tests and `COVERAGE_RESULTS=test-results/results.json npx vitest run src/lib/coverage.test.ts`: every
spec the manifest relies on must have run, with no failed test and no silent skip. A new screen, button or status without a test
fails the suite; removing a test removes coverage, and that fails it too.

## Things the stack does that a spec has to allow for

- The demo's watcher polls every 10 s and holds back a second heal for the same drift for two polls; ask it with `stack.checkNow`.
  It also heals by itself on that poll, so after changing a self-heal rule's sink, ask and then wait for **where it ends**: the
  value back, a new `HealSucceeded` (or a new self-heal task), the page Synced. Do not assert the sink just after the change, or
  what `checkNow` answered: the poll may have been first. That the watcher heals once per drift, and again for a new one, is
  tested in the watcher's own Go tests.
- The Tasks and Events pages list the newest 300 and 500: a spec finds its own rows by id or with the page's filters.
- The Playground goes through the gateway's CLI API with a Git personal access token (`Gitea.tokenFor`), and judges an
  environment by the rule in the gateway's own configuration.
- Behind the UI image a stopped service is a bad gateway ("Not ready"); reached directly it is "Unreachable".
