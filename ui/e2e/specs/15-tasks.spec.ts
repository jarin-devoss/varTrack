import { expect, test, as, unique } from '../fixtures';
import { CONFIG_REPO, Gitea } from '../fixtures/gitea';
import { TasksPage } from '../pages/TasksPage';

/**
 * The Tasks page: every sync, newest first, with filters, a lookup by ID, rows that open to their files, and a list that
 * follows a task started elsewhere. The spec starts one task of each kind it needs (works, dry run, fails) through the API
 * as the admin, then reads them as a reader.
 * covers: route:/tasks, action:tasks.filter.state, action:tasks.filter.trigger, action:tasks.search, action:tasks.clear,
 * action:tasks.statusbar, action:tasks.lookup, action:tasks.row.open, action:tasks.link.task, action:tasks.link.rule,
 * state:tasks.empty-filter, state:task.dryrun, state:task.failed, state:task.succeeded, state:tasks.live
 */
const RULE = 'gitea/redis-string'; // strict_validation; develop reads app-settings.yaml on develop

/** The tasks the test reads, made (once per run) by the `made` fixture. */
let ids = { ok: '', dry: '', failed: '' };

test.describe('reading the tasks', () => {
  test.use(as('reader'));

  test.beforeEach(async ({ page, made }) => {
    ids = { ok: await made.ok(), dry: await made.dry(), failed: (await made.failed()).id };
    await new TasksPage(page).open();
  });

  test('lists the tasks newest first with their state, and the count says how many', async ({ page, api }) => {
    const tasks = new TasksPage(page);
    const all = await api.tasks(300);
    expect(all.length).toBeGreaterThan(2);
    await expect(tasks.count).toContainText(`${all.length} tasks`);
    const listed = await tasks.listed();
    expect(listed.slice(0, 5)).toEqual(all.slice(0, 5).map(t => t.taskId.slice(0, 8)));

    await expect(tasks.row(ids.ok)).toContainText('Succeeded');
    await expect(tasks.row(ids.failed)).toContainText('Failed');
    await expect(tasks.row(ids.dry)).toContainText('Dry run');
  });

  test('the State filter keeps one state, the status bar offers the same filter, and both live in the URL', async ({ page, api }) => {
    const tasks = new TasksPage(page);
    const failed = (await api.tasks(300)).filter(t => t.state === 'FAILED');
    expect(failed.length).toBeGreaterThan(0);

    await tasks.filter('State').selectOption({ label: 'Failed' });
    await expect(page).toHaveURL(/[?&]state=FAILED/);
    await expect(tasks.count).toContainText(`${failed.length} task`);
    await expect(tasks.rows.locator('.pill', { hasText: /^Failed$/ })).toHaveCount(failed.length);
    await expect(tasks.row(ids.failed)).toBeVisible();
    await expect(tasks.row(ids.ok)).toHaveCount(0);

    await tasks.clearFilters.click();
    await expect(page).not.toHaveURL(/state=/);
    await page.locator('.status-chart__legend').getByRole('button', { name: /^Succeeded/ }).click();
    await expect(page).toHaveURL(/[?&]state=SUCCEEDED/);
    await expect(tasks.row(ids.ok)).toBeVisible();
  });

  test('the Trigger filter keeps the tasks started from the UI', async ({ page, api }) => {
    const tasks = new TasksPage(page);
    const manual = (await api.tasks(300)).filter(t => t.trigger === 'ui');
    expect(manual.length).toBeGreaterThan(0);
    await tasks.filter('Trigger').selectOption({ label: 'Manual' });
    await expect(page).toHaveURL(/[?&]trigger=ui/);
    await expect(tasks.count).toContainText(`${manual.length} task`);
    await expect(tasks.row(ids.ok)).toBeVisible();
  });

  test('the Trigger filter offers all four ways a task starts, and each lists the task that was started that way', async ({ page, api, adminApi, gitea, stack, stores }) => {
    const tasks = new TasksPage(page);
    // one task of each kind, made here: a push, the CLI, the UI (above), and a self-heal the watcher is asked to make
    const settings = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', 'develop'))!;
    const started = {
      webhook: await stack.push('redis-string', 'develop', await gitea.head(CONFIG_REPO, 'develop')),
      cli: await adminApi.cliDryRun(await Gitea.tokenFor('demo-admin', 'VarTrack123'), 'redis-string', 'default', settings),
      ui: ids.ok,
      self_heal: '',
    };
    const [seed] = await adminApi.sync('gitea/mongo', ['default']);
    await adminApi.succeeded(seed);
    stores.mongoSet('ui-mongo', 'default', 'api.rate_limit', unique('outside'));
    expect((await stack.checkNow('gitea', 'mongo'))[0].healed).toBe(true);
    await expect.poll(async () => {
      started.self_heal = (await adminApi.tasks(40)).find(t => t.trigger === 'self_heal')?.taskId ?? '';
      return started.self_heal;
    }, { timeout: 30_000, message: 'the heal is a task' }).not.toBe('');
    for (const id of [started.webhook, started.cli, started.self_heal]) await adminApi.finished(id);

    await tasks.open();
    const options = await tasks.filter('Trigger').locator('option').allInnerTexts();
    expect(options).toEqual(['Any trigger', 'Git push', 'CLI', 'Self-heal', 'Manual']);
    for (const [label, trigger] of [['Git push', 'webhook'], ['CLI', 'cli'], ['Self-heal', 'self_heal'], ['Manual', 'ui']] as const) {
      const expected = (await api.tasks(300)).filter(t => t.trigger === trigger);
      await tasks.filter('Trigger').selectOption({ label });
      await expect(page).toHaveURL(new RegExp(`[?&]trigger=${trigger}`));
      await expect(tasks.count).toContainText(`${expected.length} task`);
      await expect(tasks.row(started[trigger])).toBeVisible();
    }
  });

  test('searching matches the task ID, the rule and the environment, and says so when nothing matches', async ({ page }) => {
    const tasks = new TasksPage(page);
    await tasks.search.fill(ids.failed.slice(0, 8));
    await expect(tasks.rows).toHaveCount(1);
    await expect(tasks.row(ids.failed)).toBeVisible();

    await tasks.search.fill('redis-string');
    await expect(tasks.rows.first()).toContainText('redis-string');
    for (const row of await tasks.rows.all()) await expect(row).toContainText('redis-string');

    await tasks.search.fill('no task was ever started like this');
    await expect(page.getByText('No tasks match')).toBeVisible();
    await expect(tasks.rows).toHaveCount(0);

    await tasks.clearFilters.click();
    await expect(tasks.rows.first()).toBeVisible();
    await expect(tasks.search).toHaveValue('');
  });

  test('a row opens to the pipeline stages and files, and offers the task and the rule', async ({ page }) => {
    const tasks = new TasksPage(page);
    await tasks.toggle(ids.failed).click();
    const details = tasks.row(ids.failed).locator('.rowx__details');
    await expect(details.getByLabel('Pipeline stages')).toContainText('Parse & validate');
    await expect(details.getByText(ids.failed, { exact: true })).toBeVisible();
    await expect(details.getByLabel('Pipeline stages')).toContainText('Parse & validate(failed)');
    await expect(details.getByLabel('Pipeline stages')).toContainText('Write to sink(skipped)');

    await expect(details.getByRole('link', { name: 'Open rule' })).toHaveAttribute('href', `/rules/demo/${RULE}`);
    await details.getByRole('link', { name: 'Open task' }).click();
    await expect(page).toHaveURL(new RegExp(`/tasks/${ids.failed}`));
  });

  test('the name of a row opens the task', async ({ page }) => {
    const tasks = new TasksPage(page);
    await tasks.row(ids.ok).getByRole('link').first().click();
    await expect(page).toHaveURL(new RegExp(`/tasks/${ids.ok}`));
  });

  test('Open task by ID opens that task, and is off until something is typed', async ({ page }) => {
    const tasks = new TasksPage(page);
    await expect(tasks.lookupOpen).toBeDisabled();
    await tasks.lookupInput.fill(ids.dry);
    await expect(tasks.lookupOpen).toBeEnabled();
    await tasks.lookupInput.press('Enter');
    await expect(page).toHaveURL(new RegExp(`/tasks/${ids.dry}`));
  });

  test('a task started by someone else appears in the list without reloading', async ({ page, adminApi }) => {
    const tasks = new TasksPage(page);
    const before = await tasks.listed();
    const [started] = await adminApi.sync(RULE, ['develop'], { dryRun: true });
    expect(before).not.toContain(started.slice(0, 8));
    // the page polls every few seconds
    await expect(tasks.row(started)).toBeVisible({ timeout: 20_000 });
  });
});
