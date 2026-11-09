import { expect, test, as, unique, type Role } from '../fixtures';
import { CONFIG_REPO } from '../fixtures/gitea';
import { forgetLeftover, leaveBehind } from '../fixtures/world';
import { RulePage } from '../pages/RulePage';

/**
 * Every kind of environment × every role, pressing the real button. Which branch feeds an environment used to be
 * answered three ways (the ETL, self-heal and the permission engine), so a Sync that worked for self-heal failed in
 * the UI with "can't tell which branch feeds main". This is the spec that would have caught it.
 *
 * covers: action:rule.sync, action:env.sync, action:syncpanel.start, action:syncpanel.select-all, state:sync.*, state:file.ok
 */

interface Kind {
  name: string;
  /** The rule, `platform/datasource`, and the environment it feeds. */
  rule: string;
  env: string;
  /** The branch that feeds it, and whether the writer may push to it (main is protected). */
  branch: string;
  writerMayPush: boolean;
  /** Whether the writer can sync anything of the rule: a rule whose only environments are fed by main gives the writer dry runs only. */
  writerSyncsRule: boolean;
  /** The file the rule reads, for the value pushed (rules that read team files have no key to compare). */
  file: string;
  keyed: boolean;
}

const KINDS: Kind[] = [
  { name: 'a branch_map environment, fed by an open branch', rule: 'gitea/redis-hash', env: 'staging', branch: 'develop', writerMayPush: true, writerSyncsRule: true, file: 'app-settings.yaml', keyed: true },
  { name: 'a branch_map environment, fed by the protected branch', rule: 'gitea/redis-hash', env: 'production', branch: 'main', writerMayPush: false, writerSyncsRule: true, file: 'app-settings.yaml', keyed: true },
  { name: 'a branch_as_env environment, an open branch', rule: 'gitea/redis-string', env: 'develop', branch: 'develop', writerMayPush: true, writerSyncsRule: true, file: 'app-settings.yaml', keyed: true },
  { name: 'a branch_as_env environment, the protected branch', rule: 'gitea/redis-string', env: 'main', branch: 'main', writerMayPush: false, writerSyncsRule: true, file: 'app-settings.yaml', keyed: true },
  { name: 'a branch_map environment on a rule with no self-heal, fed by an open branch', rule: 'gitea/mongo-primary', env: 'staging', branch: 'develop', writerMayPush: true, writerSyncsRule: true, file: 'app-settings.yaml', keyed: true },
  { name: 'a branch_map environment whose only branch is the protected one', rule: 'gitea/zookeeper', env: 'production', branch: 'main', writerMayPush: false, writerSyncsRule: false, file: 'app-settings.yaml', keyed: true },
  { name: 'a file_path_map environment, fed by the rule\'s own branch', rule: 'gitea/mongo-document', env: 'team-a', branch: 'develop', writerMayPush: true, writerSyncsRule: true, file: 'team-a/app-settings.yaml', keyed: false },
];

/** The settings file with its app name changed to a value nothing else has used. */
function withName(text: string, name: string): string {
  return text.replace(/(app:\r?\n\s+name: )[^\r\n]*/, `$1${name}`);
}

async function push(gitea: { read: Function; put: Function }, kind: Kind, value: string): Promise<void> {
  const current = (await gitea.read(CONFIG_REPO, kind.file, kind.branch)) as string;
  const next = kind.keyed ? withName(current, value) : `${current.trimEnd()}\nnote: ${value}\n`;
  await gitea.put(CONFIG_REPO, kind.file, next, `ui-e2e: ${kind.rule} ${kind.env}`, kind.branch);
}

const ROLES: Role[] = ['admin', 'writer', 'reader'];

for (const kind of KINDS) {
  test.describe(kind.name, () => {
    for (const role of ROLES) {
      test.describe(`as ${role}`, () => {
        test.use(as(role));

        const mayPush = role === 'admin' || (role === 'writer' && kind.writerMayPush);

        // A reader, and a writer of a rule whose environments are all fed by a branch the writer may not push to, may
        // dry-run and not sync (the permission engine says no environment of the rule can be synced): their Sync is
        // disabled, with the reason, and Dry run is its own button.
        const dryRunOnly = role === 'reader' || (role === 'writer' && !kind.writerSyncsRule);

        if (dryRunOnly) {
          test(`only a dry run: Sync says why it is off, the dry run shows its log, and writes nothing (${kind.rule} ${kind.env})`, async ({ page, api, gitea }) => {
            const value = unique('reader');
            await push(gitea, kind, value);
            const before = kind.keyed ? (await api.keys(kind.rule, kind.env)).keys.find(k => k.key === 'app.name')?.liveValue : undefined;

            const rule = new RulePage(page);
            await rule.open(kind.rule);
            await expect(rule.envSync(kind.env)).toBeDisabled();
            await expect(rule.envSync(kind.env)).toHaveAccessibleDescription(/You can read this rule's repository but not write to it/);
            await rule.envDryRun(kind.env).click();
            await expect(rule.sync.dialog).toBeVisible();
            await expect(rule.sync.dryRun).toHaveCount(0);
            await expect(rule.sync.dialog).toContainText("You can read this rule's repository but not write to it");
            const [taskId] = await rule.sync.startAndCollect();
            await expect(rule.sync.verdicts.first()).toBeVisible();
            await expect(rule.sync.syncThese).toHaveCount(0);

            const task = await api.finished(taskId);
            expect(task.state).toBe('SUCCEEDED');
            expect(task.dryRun).toBe(true);
            if (kind.keyed) {
              const after = (await api.keys(kind.rule, kind.env)).keys.find(k => k.key === 'app.name')?.liveValue;
              expect(after, 'a dry run writes nothing').toBe(before);
            }
          });
          return;
        }

        if (mayPush) {
          test(`syncs: the task succeeds and the sink holds what was pushed (${kind.rule} ${kind.env})`, async ({ page, api, gitea }) => {
            const value = unique(role);
            await push(gitea, kind, value);

            const rule = new RulePage(page);
            await rule.open(kind.rule);
            // The environment says which branch feeds it before anything is pressed.
            await expect(rule.envSync(kind.env)).toBeEnabled();
            await expect(rule.envSync(kind.env)).toHaveAttribute('title', new RegExp(`from ${kind.branch}$`));

            const taskId = await rule.syncEnv(kind.env, { dryRun: false });
            const task = await api.finished(taskId);
            expect(task.state, task.message).toBe('SUCCEEDED');
            expect(task.dryRun).toBeFalsy();
            expect(task.files?.map(f => f.file)).toContain(kind.file);
            expect(task.files?.every(f => f.status === 'ok')).toBe(true);

            if (kind.keyed) {
              await expect
                .poll(async () => (await api.keys(kind.rule, kind.env)).keys.find(k => k.key === 'app.name')?.liveValue, { timeout: 15_000 })
                .toBe(value);
            }
          });
          return;
        }

        test(`is refused for the branch it may not push to, and nothing is written (${kind.rule} ${kind.env})`, async ({ page, api, gitea }) => {
          const value = unique('refused');
          await push(gitea, kind, value);
          const before = (await api.keys(kind.rule, kind.env)).keys.find(k => k.key === 'app.name')?.liveValue;
          const tasksBefore = (await api.tasks()).length;

          const rule = new RulePage(page);
          await rule.open(kind.rule);
          await rule.envSync(kind.env).click();
          await expect(rule.sync.dialog).toBeVisible();
          await rule.sync.dryRun.setChecked(false);
          await rule.sync.start.click();
          // A rule that prunes asks to type "prune" before it syncs a production-like environment (here the
          // protected branch's); the person types it, and then Git's refusal is what they read.
          const confirm = page.getByRole('dialog', { name: 'Prune production keys?' });
          await expect(confirm.or(rule.sync.error)).toBeVisible();
          if (await confirm.isVisible()) {
            await confirm.getByRole('textbox').fill('prune');
            await confirm.getByRole('button', { name: 'Sync and prune' }).click();
          }

          await expect(rule.sync.error).toContainText(`may not push to ${kind.branch}`);
          await expect(rule.sync.error).toContainText(`the branch that feeds ${kind.env}`);
          const after = (await api.keys(kind.rule, kind.env)).keys.find(k => k.key === 'app.name')?.liveValue;
          expect(after, 'a refused sync writes nothing').toBe(before);
          expect((await api.tasks()).length, 'a refused sync queues nothing').toBe(tasksBefore);
        });
      });
    }
  });
}

test.describe('an environment no branch feeds any more', () => {
  // The spec makes its own: gitea/redis-hash is fed `main` from a branch for a moment, then put back, so its `main` environment
  // is written and kept (with its keys and history) but the rule as it is now produces nothing that lands there. (The suite's
  // setup forgets every leftover, so none is here before this.)
  const RULE = 'gitea/redis-hash';
  const STALE = 'main';

  test.beforeAll(async ({}, info) => {
    test.setTimeout(240_000);
    await leaveBehind(info.project.use.baseURL ?? 'http://localhost:8080', 'redis-hash', STALE, 'develop');
  });
  test.afterAll(async ({}, info) => {
    await forgetLeftover(info.project.use.baseURL ?? 'http://localhost:8080', RULE, STALE);
  });

  for (const role of ROLES) {
    test.describe(`as ${role}`, () => {
      test.use(as(role));

      test('it is listed, labelled, and Sync is disabled with the reason', async ({ page, api }) => {
        const target = (await api.rule(RULE)).targets.find(t => t.env === STALE);
        expect(target, 'the environment is still listed').toBeDefined();
        expect(target?.fedBy).toBeUndefined();
        expect(target?.notFed).toMatch(/^nothing feeds main in the redis-hash rule/);

        const rule = new RulePage(page);
        await rule.open(RULE);
        await expect(rule.env(STALE)).toContainText('No longer synced by this rule');
        await expect(rule.envSync(STALE)).toBeDisabled();
        await expect(rule.envSync(STALE)).toHaveAttribute('title', /nothing feeds main in the redis-hash rule/);
        // a dry run of it would read the branch that feeds it, and there is none: not offered either
        if (role === 'reader') await expect(rule.envDryRun(STALE)).toBeDisabled();
        // its keys and history are still there
        await expect(rule.envDetails(STALE)).toBeEnabled();
      });

      test('the panel does not offer it', async ({ page }) => {
        const rule = new RulePage(page);
        await rule.open(RULE);
        // the reader's Sync is off; their Dry run opens the same panel
        await (role === 'reader' ? rule.dryRunAll : rule.syncAll).click();
        await expect(rule.sync.dialog).toBeVisible();
        await expect(rule.sync.environment('staging')).toBeChecked();
        await expect(rule.sync.environment('production')).toBeChecked();
        await expect(rule.sync.environment(STALE)).toBeDisabled();
        await expect(rule.sync.environment(STALE)).not.toBeChecked();
        await expect(rule.sync.dialog).toContainText('nothing feeds main');
      });
    });
  }

  test.describe('as admin, asking the API directly', () => {
    test.use(as('admin'));

    test('is refused with the reason, for a sync and a dry run alike', async ({ api, page }) => {
      for (const dryRun of [false, true]) {
        const response = await page.request.post(`/api/orchestrator/v1/ui/rules/${RULE}/sync`, { data: { envs: [STALE], dryRun, prune: false } });
        expect(response.status(), `dryRun=${dryRun}`).toBe(403);
        expect(((await response.json()) as { detail: string }).detail).toMatch(/^permission denied: nothing feeds main in the redis-hash rule/);
      }
      expect((await api.session()).admin).toBe(true);
    });
  });
});
