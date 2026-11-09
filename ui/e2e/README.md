# UI end-to-end tests

These tests open the web UI in a browser against the real stack (Gitea, the gateway, the orchestrator and its worker, the
watcher, and the sinks) and use it the way people do: they sign in through Gitea's own page, click, type, read what the
page says, and then check the sinks and the API to see that what the page said is true.

They exist because a unit test of a component cannot see that the page tells the truth about the system behind it. The first
bug they were written for — a *Sync* that said "can't tell which branch feeds main" — lived between the UI, the orchestrator
and the gateway's permission engine, and no test of any one of them could fail.

## Run them

The way CI runs them, in one command from `ui/` (clean volumes, the 23-suite regression, the browser tests, the coverage check):

```sh
npm ci
npm run test:e2e:install      # once: the browser
npm run test:e2e:stack        # everything; --no-up for a stack that is already up, --keep to leave it running
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

Locally, where the orchestrator image cannot always be rebuilt, bring the stack up with the host's `orchestrator-service/app` (and
host-built Go binaries) bind-mounted over the images, serve `ui/dist` through the nginx image with `ui/nginx.conf.template`, and
restart `orchestrator-api` and `orchestrator-worker` after changing Python.

## How a run goes

1. `global-setup.ts` signs in once per role (admin, writer, reader, outsider) through the real "Sign in with Gitea" and
   Gitea's consent page, and saves each browser state to `e2e/.auth/<role>.json`.
2. `fixtures/world.ts` builds what the specs expect to find, the same whatever an earlier run left behind: a `develop` branch
   next to `main`, team folders, the schema repository with the CUE schemas and `world/bundle.json` (a rule for each kind of
   environment: `branch_map`, `branch_as_env`, `file_path_map`, with and without self-heal and
   strict validation), and the first syncs, pushed as signed webhooks. Every rule names its file and its environments (one that does not is refused).
   Last, it forgets every environment no rule produces any more (an earlier run's leftovers) and logs which; a spec about
   leftovers makes its own (`leaveBehind`) and forgets it after.
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
- The Tasks and Events pages list the newest 300 and 500: a spec finds its own rows by id or with the page's filters.
- The Playground goes through the gateway's CLI API with a Git personal access token (`Gitea.tokenFor`), and judges an
  environment by the rule in the gateway's own configuration.
- Behind the UI image a stopped service is a bad gateway ("Not ready"); reached directly it is "Unreachable".
