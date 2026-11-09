import { expect, test, as, WORLD } from '../fixtures';
import { CONFIG_REPO } from '../fixtures/gitea';
import { RulePage } from '../pages/RulePage';
import { RulesPage } from '../pages/RulesPage';
import { SettingsPage } from '../pages/SettingsPage';
import { TaskPage } from '../pages/TaskPage';

/**
 * What the UI does when something goes wrong: a sink refuses a write, a file in a multi-file sync fails, and each service
 * of the stack stops. The product has to say what happened and recover when the cause is gone. Whatever a spec breaks, it
 * mends in `finally`/after (the `faults` fixture starts a stopped service again).
 * covers: state:task.failed, state:file.error, state:file.rolled_back, state:file.aborted, state:file.partial, state:service.down,
 * state:sink.unreachable, state:error.retry, action:error.retry, action:error.settings
 */
test.use(as('admin'));

/** The page shows Try again until its own retry succeeds; press it when it is there. */
async function retryIfOffered(page: import('@playwright/test').Page): Promise<void> {
  const again = page.getByRole('button', { name: 'Try again' });
  if (await again.isVisible()) await again.click({ timeout: 2_000 }).catch(() => undefined);
}

test.describe('a sink refuses a write', () => {
  const RULE = 'gitea/redis-hash';
  const KEY = 'ui-redis-hash-staging:vartrack';
  const DB = 2;

  test('the sync fails, says what Redis said, and the next sync mends it', async ({ page, api, stores }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);

    // someone stored a string where the hash goes: Redis refuses to write a hash into it
    stores.redis(DB, 'DEL', KEY);
    stores.redis(DB, 'SET', KEY, 'not a hash');
    let failedTask = '';
    try {
      failedTask = await rule.syncEnv('staging', { dryRun: false });
      const task = await api.finished(failedTask);
      expect(task.state).toBe('FAILED');
      expect(task.message).toContain('WRONGTYPE');

      const taskPage = new TaskPage(page);
      await taskPage.open(failedTask);
      await expect(taskPage.hero).toHaveClass(/hero--red/);
      await expect(taskPage.stages).toContainText('Write to sink(failed)');
      const row = taskPage.files.getByRole('row').filter({ hasText: 'app-settings.yaml' });
      await expect(row.locator('.pill')).toHaveText('Error');
      await expect(row).toContainText('WRONGTYPE');

      // the rule page and the events page tell the same story
      await rule.open(RULE);
      await expect(page.locator('main')).toContainText('Sync failed');
      await expect(page.locator('main')).toContainText('Redis refused 1 of 2 commands of the write: WRONGTYPE');
      await page.goto('/events?rule=gitea%2Fredis-hash');
      await expect(page.locator('.rowx').filter({ hasText: 'Sync failed' }).first()).toContainText('WRONGTYPE');
    } finally {
      stores.redis(DB, 'DEL', KEY);
    }

    // mended: the same button, and the environment is as Git says again
    await rule.open(RULE);
    const mended = await api.finished(await rule.syncEnv('staging', { dryRun: false }));
    expect(mended.state, mended.message).toBe('SUCCEEDED');
    expect(Number(stores.redis(DB, 'HLEN', KEY))).toBeGreaterThan(10);
  });
});

test.describe('one file of a multi-file sync is refused', () => {
  const COLLECTION = 'ui-mongo-document';

  test('the file written before it is rolled back, and the task page shows each with its reason', async ({ page, api, gitea, stack, stores }) => {
    // a collection that refuses team-b's document, as a schema validator on the database would
    stores.mongo(`db.runCommand({ collMod: ${JSON.stringify(COLLECTION)}, validator: { $expr: { $ne: ['$_vt_env', 'team-b'] } }, validationLevel: 'strict', validationAction: 'error' })`);
    try {
      const sha = await gitea.head(CONFIG_REPO, WORLD.openBranch);
      const taskId = await stack.push('mongo-document', WORLD.openBranch, sha, ['team-a/app-settings.yaml', 'team-b/app-settings.yaml']);
      const task = await api.finished(taskId);
      expect(task.state).toBe('FAILED');

      const files = Object.fromEntries((task.files ?? []).map(f => [f.file, f.status]));
      // the database's own document validation refused it; the sink reports that as a rejected file
      expect(files['team-b/app-settings.yaml'], 'the refused file').toBe('validation_failed');
      expect(files['team-a/app-settings.yaml'], 'the file written before it is taken back').toMatch(/^(rolled_back|partial)$/);

      const taskPage = new TaskPage(page);
      await taskPage.open(taskId);
      await expect(taskPage.hero).toHaveClass(/hero--red/);
      const fileRow = (name: string) => taskPage.files.getByRole('row').filter({ has: page.getByRole('cell', { name, exact: true }) });
      const a = fileRow('team-a/app-settings.yaml');
      const b = fileRow('team-b/app-settings.yaml');
      await expect(a.locator('.pill')).toHaveText(files['team-a/app-settings.yaml'] === 'rolled_back' ? 'Rolled back' : 'Left written');
      await expect(a).toContainText('rolled back: team-b/app-settings.yaml failed');
      await expect(b.locator('.pill')).toHaveText('Invalid');
      await expect(b).toContainText('Document failed validation');
    } finally {
      stores.mongo(`db.runCommand({ collMod: ${JSON.stringify(COLLECTION)}, validator: {}, validationLevel: 'off', validationAction: 'error' })`);
    }

    // with the cause gone, a sync of the rule writes both
    const [a, b] = await api.sync('gitea/mongo-document', ['team-a', 'team-b']);
    expect((await api.finished(a)).state).toBe('SUCCEEDED');
    expect((await api.finished(b)).state).toBe('SUCCEEDED');
  });
});

test.describe('a file before the refused one is not reached', () => {
  const COLLECTION = 'ui-mongo-document';

  test('the files after a refused file are Not reached', async ({ page, api, gitea, stack, stores }) => {
    // now it is team-a that the collection refuses: team-b comes after it
    stores.mongo(`db.runCommand({ collMod: ${JSON.stringify(COLLECTION)}, validator: { $expr: { $ne: ['$_vt_env', 'team-a'] } }, validationLevel: 'strict', validationAction: 'error' })`);
    try {
      const sha = await gitea.head(CONFIG_REPO, WORLD.openBranch);
      const taskId = await stack.push('mongo-document', WORLD.openBranch, sha, ['team-a/app-settings.yaml', 'team-b/app-settings.yaml']);
      const task = await api.finished(taskId);
      expect(task.state).toBe('FAILED');
      const files = Object.fromEntries((task.files ?? []).map(f => [f.file, f.status]));
      expect(files['team-a/app-settings.yaml']).toBe('validation_failed');
      expect(files['team-b/app-settings.yaml'], 'the file after the refused one').toBe('aborted');

      const taskPage = new TaskPage(page);
      await taskPage.open(taskId);
      const fileRow = (name: string) => taskPage.files.getByRole('row').filter({ has: page.getByRole('cell', { name, exact: true }) });
      await expect(fileRow('team-b/app-settings.yaml').locator('.pill')).toHaveText('Not reached');
    } finally {
      stores.mongo(`db.runCommand({ collMod: ${JSON.stringify(COLLECTION)}, validator: {}, validationLevel: 'off', validationAction: 'error' })`);
    }
    const [a, b] = await api.sync('gitea/mongo-document', ['team-a', 'team-b']);
    expect((await api.finished(a)).state).toBe('SUCCEEDED');
    expect((await api.finished(b)).state).toBe('SUCCEEDED');
  });
});

test.describe('a service of the stack stops', () => {
  test('a sink that hangs: the task is Running while it waits, and finishes by itself when the sink answers', async ({ page, faults }) => {
    const rule = new RulePage(page);
    await rule.open('gitea/zookeeper');
    faults.pause('zookeeper');

    const taskId = await rule.syncEnv('production', { dryRun: false });
    await expect.poll(async () => {
      const response = await page.request.get(`/api/orchestrator/v1/ui/tasks/${taskId}`);
      return response.status() === 200 ? ((await response.json()) as { state: string }).state : 'not recorded';
    }, { timeout: 30_000, message: 'a worker has the task' }).toBe('RUNNING');

    const taskPage = new TaskPage(page);
    await taskPage.open(taskId);
    await expect(taskPage.hero).toHaveClass(/hero--blue/);
    await expect(taskPage.stages).toContainText('Parse & validate(in progress)');
    await expect(taskPage.fact('Duration')).toContainText('(running)');

    faults.unpause('zookeeper');
    // the open page follows the task to its end, without a reload
    await expect(taskPage.hero).toHaveClass(/hero--green/, { timeout: 60_000 });
    await expect(taskPage.stages).toContainText('Write to sink(done)');
  });

  test('orchestrator: the lists say they could not load, the page offers Try again, and it recovers when the service is back', async ({ page, faults }) => {
    const rules = new RulesPage(page);
    await rules.open();
    faults.stop('orchestrator-api');

    await rules.refresh.click();
    // the last list stays on screen; a page opened now has nothing to show and says so
    await page.goto('/environments');
    await expect(page.getByText(/Couldn't load|couldn't|unavailable/i).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('link', { name: 'Connection settings' }).first()).toBeVisible();

    await faults.start('orchestrator-api');
    // live updates retry on their own; a person who does not wait presses Try again. Either way the list comes back.
    await retryIfOffered(page);
    await expect(page.locator('.rowx').first()).toBeVisible({ timeout: 30_000 });
  });

  test('worker: a sync is accepted and waits, the task page does not lie, and it finishes by itself when the worker is back', async ({ page, api, faults }) => {
    const rule = new RulePage(page);
    await rule.open('gitea/redis-string');
    faults.stop('orchestrator-worker');

    const taskId = await rule.syncEnv('develop', { dryRun: true });
    const taskPage = new TaskPage(page);
    await taskPage.open(taskId);
    // queued, not failed and not "not found" with no explanation
    await expect(page.getByText(/queued|Waiting|Pending|Task not found/i).first()).toBeVisible();
    await expect(taskPage.hero).not.toHaveClass(/hero--red/);

    await faults.start('orchestrator-worker');
    expect((await api.finished(taskId, 90_000)).state).toBe('SUCCEEDED');
    await page.reload();
    await expect(taskPage.hero).toHaveClass(/hero--green/);
  });

  test('a sink: its environments read Unknown and say the sink cannot be read, a sync fails with the reason, and both mend when it is back', async ({ page, api, faults }) => {
    const RULE = 'gitea/mongo-primary';
    const rule = new RulePage(page);
    await rule.open(RULE);
    await expect(rule.env('staging')).toHaveAccessibleName(/staging: Synced/);

    faults.stop('mongo');
    // The datasource's connect_timeout_ms (5 s in config.cue) bounds how long the driver looks for the server, so the read
    // fails within seconds; Refresh reads the sink now, not the last answer.
    await expect.poll(async () => (await api.fresh(RULE)).targets.find(t => t.env === 'staging')?.drift, { timeout: 40_000, intervals: [2_000] }).toBe('unknown');
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(rule.env('staging')).toHaveAccessibleName(/staging: Unknown/, { timeout: 30_000 });

    const taskId = await rule.syncEnv('staging', { dryRun: false });
    const task = await api.finished(taskId, 60_000);
    expect(task.state).toBe('FAILED');
    await new TaskPage(page).open(taskId);
    await expect(page.locator('.hero')).toHaveClass(/hero--red/);

    await faults.start('mongo');
    await expect.poll(async () => (await api.fresh(RULE)).targets.find(t => t.env === 'staging')?.drift, { timeout: 40_000, intervals: [2_000] }).toBe('in_sync');
    await rule.open(RULE);
    await expect(rule.env('staging')).toHaveAccessibleName(/staging: Synced/);
  });

  test('gateway: pages that need a permission answer say permissions are unavailable, and the stack mends when it is back', async ({ page, faults }) => {
    const rules = new RulesPage(page);
    await rules.open();
    faults.stop('gateway');

    await page.goto('/rules');
    await expect(page.getByText(/permissions are unavailable|Couldn't load/i).first()).toBeVisible({ timeout: 30_000 });

    await faults.start('gateway');
    await retryIfOffered(page);
    await expect(page.locator('article.rcard').first()).toBeVisible({ timeout: 30_000 });
  });

  test('watcher: Settings shows it unreachable while the rest keeps working', async ({ page, faults }) => {
    const settings = new SettingsPage(page);
    faults.stop('watcher');
    await settings.open();
    // behind the UI's proxy a stopped watcher is a bad gateway ("Not ready"); reached directly it is "Unreachable". Either way not Ready.
    await expect(settings.panel('Services').getByRole('row').filter({ hasText: /^watcher/i })).toContainText(/Unreachable|Not ready/, { timeout: 20_000 });
    await expect(settings.panel('Services').getByRole('row').filter({ hasText: /^orchestrator/i })).toContainText('Ready');

    await page.goto('/rules');
    await expect(page.locator('article.rcard').first()).toBeVisible();

    await faults.start('watcher');
    await settings.open();
    await expect(settings.panel('Services').getByRole('row').filter({ hasText: /^watcher/i })).toContainText('Ready', { timeout: 30_000 });
  });
});
