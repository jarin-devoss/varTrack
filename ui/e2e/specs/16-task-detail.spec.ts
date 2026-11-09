import { expect, test, as } from '../fixtures';
import { CONFIG_REPO } from '../fixtures/gitea';
import { TaskPage } from '../pages/TaskPage';

/**
 * One task: its hero, the pipeline stages, the facts (who, from where, which revision, how long, what came of it) and
 * the files it wrote or checked. The tasks it reads are made by the `made` fixture (a sync that works, a dry run, one a strict
 * schema refuses), and read as a reader. covers: route:/tasks/:id, action:task.copyid, action:task.link.rule, action:task.link.env, action:task.back,
 * state:task.succeeded, state:task.dryrun, state:task.failed, state:task.notfound, state:file.written, state:file.checked
 */
const RULE = 'gitea/redis-string';

/** The tasks the test reads, made (once per run) by the `made` fixture. */
let made = { ok: '', dry: '', failed: '' };
let BAD = '';

test.describe('reading a task', () => {
  test.use(as('reader'));

  test.beforeEach(async ({ made: fixture }) => {
    const failed = await fixture.failed();
    made = { ok: await fixture.ok(), dry: await fixture.dry(), failed: failed.id };
    BAD = failed.bad;
  });

  test('a sync that worked: the hero, every stage done, the facts, and the file written', async ({ page, api }) => {
    const task = new TaskPage(page);
    await task.open(made.ok);
    const recorded = await api.task(made.ok);

    await expect(page).toHaveTitle(`Task ${made.ok.slice(0, 8)} · varTrack`);
    await expect(task.hero).toHaveClass(/hero--green/);
    await expect(task.kind).toHaveText('Task · Manual');
    await expect(task.name).toContainText(made.ok.slice(0, 8));
    await expect(task.stages).toContainText('Received(done)');
    await expect(task.stages).toContainText('Parse & validate(done)');
    await expect(task.stages).toContainText('Write to sink(done)');

    await expect(task.fact('Task ID')).toHaveText(made.ok);
    await expect(task.fact('Source')).toHaveText('vartrack-demo/demo-configs@develop');
    await expect(task.fact('Revision')).toContainText(recorded.revision!.slice(0, 7));
    await expect(task.fact('Duration')).toHaveText(/\d/);
    await expect(task.fact('Result')).toHaveText(/\d+ keys written/);

    await expect(page.getByText('1 file', { exact: true })).toBeVisible();
    const row = task.files.getByRole('row').filter({ hasText: 'app-settings.yaml' });
    await expect(row).toContainText('develop');
    await expect(row.locator('.pill')).toHaveText('Written');
    await expect(task.files.getByRole('columnheader', { name: 'Written' })).toBeVisible();
    await expect(row).toContainText(/validation: ok/);
    await expect(row).toContainText('app-settings.yaml.cue (name)');
  });

  test('a dry run says it is one, counts keys checked and writes nothing', async ({ page }) => {
    const task = new TaskPage(page);
    await task.open(made.dry);
    await expect(task.hero).toHaveClass(/hero--green/);
    await expect(task.kind).toHaveText('Task · Manual · dry run');
    await expect(task.stages).toContainText('Simulate (no writes)(done)');
    await expect(task.fact('Result')).toHaveText(/\d+ keys checked, nothing written/);
    await expect(task.files.getByRole('columnheader', { name: 'Keys' })).toBeVisible();
    await expect(task.files.getByRole('columnheader', { name: 'Written' })).toHaveCount(0);
    await expect(task.files.locator('.pill').first()).toHaveText('Checked');
  });

  test('a sync a strict schema refused: red, validation failed, nothing skipped over, and the message names the key and the value', async ({ page }) => {
    const task = new TaskPage(page);
    await task.open(made.failed);
    await expect(task.hero).toHaveClass(/hero--red/);
    await expect(task.stages).toContainText('Parse & validate(failed)');
    await expect(task.stages).toContainText('Write to sink(skipped)');
    await expect(task.fact('Result')).toContainText('strict_validation');
    await expect(task.fact('Result')).toContainText('observability.log_level');
    await expect(task.fact('Result')).toContainText(BAD);
    await expect(page.getByText('No file results')).toBeVisible();
  });

  test('the rule and the environment in the hero link back to where the task ran', async ({ page }) => {
    const task = new TaskPage(page);
    await task.open(made.ok);
    await expect(task.hero.getByRole('link', { name: RULE })).toHaveAttribute('href', `/rules/demo/${RULE}`);
    await task.hero.getByRole('link', { name: 'develop' }).click();
    await expect(page).toHaveURL(/\/rules\/demo\/gitea\/redis-string\?env=develop/);
    await expect(page.getByRole('tablist', { name: 'Environment', exact: true })).toBeVisible();
  });

  test('Copy task ID puts the whole ID on the clipboard', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    const task = new TaskPage(page);
    await task.open(made.ok);
    await task.copyId.click();
    await expect(page.getByRole('button', { name: 'Copy task ID' })).toHaveAttribute('title', 'Copied');
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(made.ok);
  });

  test('an ID that no task has says so, and offers the list', async ({ page }) => {
    const task = new TaskPage(page);
    await task.open('00000000-0000-0000-0000-000000000000');
    await expect(page.getByText('Task not found')).toBeVisible();
    await expect(page.getByText('No task with ID')).toContainText('00000000-0000-0000-0000-000000000000');
    await page.getByRole('link', { name: 'All tasks' }).click();
    await expect(page).toHaveURL(/\/tasks$/);
  });
});

test.describe('a file that holds no keys', () => {
  test.use(as('admin'));

  test('is Skipped: the task succeeds, writes nothing, and the page says the file was skipped', async ({ page, adminApi, gitea, stores }) => {
    const good = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', 'develop'))!;
    const key = 'ui-redis-string-develop:observability_log_level';
    const before = stores.redisGet(3, key);
    let taskId = '';
    try {
      await gitea.put(CONFIG_REPO, 'app-settings.yaml', '# nothing here yet\n', 'ui-e2e: a file with no keys', 'develop');
      [taskId] = await adminApi.sync(RULE, ['develop']);
      expect((await adminApi.finished(taskId)).state).toBe('SUCCEEDED');

      const task = new TaskPage(page);
      await task.open(taskId);
      await expect(task.hero).toHaveClass(/hero--green/);
      await expect(task.fact('Result')).toHaveText('0 keys written');
      const row = task.files.getByRole('row').filter({ hasText: 'app-settings.yaml' });
      await expect(row.locator('.pill')).toHaveText('Skipped');
      expect(stores.redisGet(3, key), 'nothing was written, and nothing deleted').toBe(before);
    } finally {
      await gitea.put(CONFIG_REPO, 'app-settings.yaml', good, 'ui-e2e: put the file back', 'develop');
      const [restore] = await adminApi.sync(RULE, ['develop']);
      await adminApi.finished(restore);
    }
  });
});

