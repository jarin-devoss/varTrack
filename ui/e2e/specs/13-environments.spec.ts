import { expect, test, as } from '../fixtures';
import { EnvironmentsPage } from '../pages/EnvironmentsPage';

/**
 * The Environments page: every environment of every rule the person can see, one row each, with the filters of the Rules
 * page and a row that opens to the comparison and the actions on that environment.
 * covers: route:/environments, action:envs.filter.sync, action:envs.filter.sink, action:envs.filter.env, action:envs.search,
 * action:envs.clear, action:envs.refresh, action:envs.statusbar, action:envs.row.open, action:envs.row.sync, action:envs.row.diff,
 * action:envs.row.current, state:envs.empty-filter
 */
test.describe('as reader', () => {
  test.use(as('reader'));

  test.beforeEach(async ({ page }) => {
    await new EnvironmentsPage(page).open();
  });

  test('lists one row per environment of every visible rule, and the count says how many', async ({ page, api }) => {
    const envs = new EnvironmentsPage(page);
    const expected = (await api.rules()).flatMap(r => r.targets.map(t => `${r.name} ${t.env}`)).sort();
    expect(expected.length).toBeGreaterThan(0);
    expect((await envs.listed()).sort()).toEqual(expected);
    await expect(envs.count).toContainText(`${expected.length} environments`);
  });

  test('the Environment filter keeps the rows of that environment and is written to the URL', async ({ page, api }) => {
    const envs = new EnvironmentsPage(page);
    const staging = (await api.rules()).flatMap(r => r.targets.filter(t => t.env === 'staging').map(t => `${r.name} ${t.env}`)).sort();
    expect(staging.length).toBeGreaterThan(0);
    await envs.filter('Environment').selectOption('staging');
    await expect(page).toHaveURL(/[?&]env=staging/);
    expect((await envs.listed()).sort()).toEqual(staging);
  });

  test('the Sink filter keeps the environments of rules writing to that sink', async ({ page, api }) => {
    const envs = new EnvironmentsPage(page);
    const redis = (await api.rules())
      .filter(r => (r as unknown as { sinkType: string }).sinkType === 'redis')
      .flatMap(r => r.targets.map(t => `${r.name} ${t.env}`))
      .sort();
    expect(redis.length).toBeGreaterThan(0);
    await envs.filter('Sink').selectOption('redis');
    await expect(page).toHaveURL(/[?&]sink=redis/);
    expect((await envs.listed()).sort()).toEqual(redis);
  });

  test('the Sync status filter and the status bar are the same filter, and Clear filters undoes both', async ({ page }) => {
    const envs = new EnvironmentsPage(page);
    const all = (await envs.listed()).length;

    await envs.filter('Sync status').selectOption('Synced');
    await expect(page).toHaveURL(/[?&]status=Synced/);
    const viaSelect = (await envs.listed()).sort();
    expect(viaSelect.length).toBeGreaterThan(0);
    expect(viaSelect.length).toBeLessThanOrEqual(all);

    await envs.clearFilters.click();
    await expect(page).not.toHaveURL(/status=/);
    await expect(envs.rows).toHaveCount(all);

    await page.locator('.status-chart__legend').getByRole('button', { name: /^Synced/ }).click();
    await expect(page).toHaveURL(/[?&]status=Synced/);
    await expect.poll(async () => (await envs.listed()).sort()).toEqual(viaSelect);
  });

  test('the status bar names how many environments are in each state', async ({ page }) => {
    const envs = new EnvironmentsPage(page);
    const total = (await envs.listed()).length;
    await expect(page.getByRole('img', { name: new RegExp(`^${total} environments:`) })).toBeVisible();
  });

  test('searching matches the rule, the environment, the destination and the repository, and says so when nothing matches', async ({ page }) => {
    const envs = new EnvironmentsPage(page);
    await envs.search.fill('redis-hash production');
    await expect(envs.row('gitea/redis-hash', 'production')).toBeVisible();
    await expect(envs.rows).toHaveCount(1);

    await envs.search.fill('ui-redis-hash-production');
    await expect(envs.row('gitea/redis-hash', 'production')).toBeVisible();

    await envs.search.fill('no-such-environment-anywhere');
    await expect(page.getByText('No environments match')).toBeVisible();
    await expect(envs.rows).toHaveCount(0);

    await envs.clearFilters.click();
    await expect(envs.rows.first()).toBeVisible();
  });

  test('Refresh asks the server again', async ({ page }) => {
    const envs = new EnvironmentsPage(page);
    const asked = page.waitForRequest(r => /\/v1\/ui\/rules(\?|$)/.test(r.url()) && r.method() === 'GET');
    await envs.refresh.click();
    await asked;
    await expect(envs.rows.first()).toBeVisible();
  });

  test('a row opens to the comparison and links to the Diff and the Current values of that environment', async ({ page }) => {
    const envs = new EnvironmentsPage(page);
    const row = envs.row('gitea/redis-hash', 'staging');
    await expect(row.locator('.rowx__details')).toHaveCount(0);
    await envs.toggle('gitea/redis-hash', 'staging').click();
    await expect(envs.toggle('gitea/redis-hash', 'staging')).toHaveAttribute('aria-expanded', 'true');
    await expect(row.locator('.rowx__details')).toBeVisible();

    await expect(row.getByRole('link', { name: 'Diff' })).toHaveAttribute('href', '/rules/demo/gitea/redis-hash?env=staging&view=diff');
    await expect(row.getByRole('link', { name: 'Current values' })).toHaveAttribute('href', '/rules/demo/gitea/redis-hash?env=staging&view=current');

    await row.getByRole('link', { name: 'Diff' }).click();
    await expect(page).toHaveURL(/\/rules\/demo\/gitea\/redis-hash\?env=staging&view=diff/);
    await expect(page.getByRole('tablist', { name: 'Environment', exact: true }).getByRole('tab', { name: 'Diff' })).toHaveAttribute('aria-selected', 'true');
  });

  test('the name of a row opens the rule with that environment', async ({ page }) => {
    const envs = new EnvironmentsPage(page);
    await envs.row('gitea/redis-hash', 'staging').getByRole('link', { name: /staging/ }).first().click();
    await expect(page).toHaveURL(/\/rules\/demo\/gitea\/redis-hash\?env=staging/);
  });

  test('a reader can open Sync on a row, but only to dry-run: the panel is a dry run and cannot be switched', async ({ page }) => {
    const envs = new EnvironmentsPage(page);
    await envs.toggle('gitea/redis-hash', 'staging').click();
    await envs.row('gitea/redis-hash', 'staging').getByRole('button', { name: /^Sync staging$/ }).click();
    await expect(envs.sync.dialog).toBeVisible();
    await expect(envs.sync.dryRun).toBeChecked();
    await expect(envs.sync.dryRun).toBeDisabled();
    await expect(envs.sync.start).toHaveText('Start dry run');
  });
});

test.describe('as admin', () => {
  test.use(as('admin'));

  test('Sync on a row starts that environment only', async ({ page, api, stores }) => {
    const envs = new EnvironmentsPage(page);
    await envs.open('?q=redis-hash');
    await envs.toggle('gitea/redis-hash', 'staging').click();
    await envs.row('gitea/redis-hash', 'staging').getByRole('button', { name: /^Sync staging$/ }).click();

    await expect(envs.sync.dialog).toBeVisible();
    await expect(envs.sync.environment('staging')).toBeChecked();
    await expect(envs.sync.environment('production')).not.toBeChecked();
    await envs.sync.dryRun.setChecked(true);
    const taskIds = await envs.sync.startAndCollect();
    expect(taskIds, 'one environment, one task').toHaveLength(1);
    const task = await api.finished(taskIds[0]);
    expect(task.state, task.message).toBe('SUCCEEDED');
    expect((task as unknown as { env?: string }).env).toBe('staging');
    expect(stores.redisKeys(2, 'ui-redis-hash-staging*').length).toBeGreaterThan(0);
  });
});
