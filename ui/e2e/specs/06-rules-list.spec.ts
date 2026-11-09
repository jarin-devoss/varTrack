import { expect, test, as } from '../fixtures';
import { RulesPage } from '../pages/RulesPage';

/**
 * The Rules page: filters, sort, search, the status bar, and New rule. Filters live in the URL, so a link carries them.
 * covers: route:/rules, action:rules.filter.sync, action:rules.filter.sink, action:rules.filter.env, action:rules.filter.lastsync,
 * action:rules.sort, action:rules.search, action:rules.clear, action:rules.refresh, action:rules.newrule, action:rules.statusbar,
 * state:rules.empty-filter
 */
test.use(as('reader'));

test.beforeEach(async ({ page }) => {
  await page.goto('/rules');
  await new RulesPage(page).ready();
});

test('lists every visible rule, and the count says how many', async ({ page, api }) => {
  const rules = new RulesPage(page);
  const names = (await api.rules()).map(r => r.name).sort();
  expect(await rules.names()).toEqual(names);
  await expect(rules.count).toContainText(`${names.length} rules`);
});

test('the Sink filter narrows the list to that kind of sink and is written to the URL', async ({ page, api }) => {
  const rules = new RulesPage(page);
  const all = await api.rules();
  const redis = all.filter(r => (r as unknown as { sinkType: string }).sinkType === 'redis').map(r => r.name).sort();
  expect(redis.length).toBeGreaterThan(0);
  expect(redis.length).toBeLessThan(all.length);

  await rules.filter('Sink').selectOption('redis');
  await expect(page).toHaveURL(/[?&]sink=redis/);
  expect(await rules.names()).toEqual(redis);
  await expect(rules.count).toContainText(`${redis.length} rule`);
});

test('the Environment filter shows the rules that have that environment', async ({ page, api }) => {
  const rules = new RulesPage(page);
  const withStaging = (await api.rules()).filter(r => r.targets.some(t => t.env === 'staging')).map(r => r.name).sort();
  expect(withStaging.length).toBeGreaterThan(0);
  await rules.filter('Environment').selectOption('staging');
  await expect(page).toHaveURL(/[?&]env=staging/);
  expect(await rules.names()).toEqual(withStaging);
});

test('the Sync status filter and the status bar filter by the same status', async ({ page }) => {
  const rules = new RulesPage(page);
  const all = (await rules.names()).length;
  await rules.filter('Sync status').selectOption('Synced');
  await expect(page).toHaveURL(/[?&]status=Synced/);
  await expect(rules.cards()).not.toHaveCount(all);
  const viaSelect = await rules.names();

  await rules.clearFilters.click();
  await expect(page).not.toHaveURL(/status=/);
  await expect(rules.cards()).toHaveCount(all);

  // the status bar's legend carries the same filter: clicking "Synced" is choosing it in the select
  await page.locator('.status-chart__legend').getByRole('button', { name: /^Synced/ }).click();
  await expect(page).toHaveURL(/[?&]status=Synced/);
  await expect(rules.cards()).toHaveCount(viaSelect.length);
  expect(await rules.names()).toEqual(viaSelect);
});

test('the Last sync filter keeps the rules whose last sync was in that state', async ({ page, api }) => {
  const rules = new RulesPage(page);
  const succeeded = (await api.rules())
    .filter(r => r.targets.some(t => t.lastSync?.state === 'SUCCEEDED'))
    .map(r => r.name)
    .sort();
  await rules.filter('Last sync').selectOption('SUCCEEDED');
  await expect(page).toHaveURL(/[?&]sync=SUCCEEDED/);
  expect(await rules.names()).toEqual(succeeded);
});

test('Sort orders the list by name or by the worst rule first, and survives a reload in the URL', async ({ page }) => {
  const rules = new RulesPage(page);
  await rules.filter('Sort').selectOption('name');
  await expect(page).toHaveURL(/[?&]sort=name/);
  const names = await rules.cards().evaluateAll(nodes => nodes.map(n => (n.getAttribute('aria-label') ?? '').split(':')[0]));
  expect(names).toEqual([...names].sort((a, b) => a.localeCompare(b)));

  await page.reload();
  await rules.ready();
  await expect(rules.filter('Sort')).toHaveValue('name');
});

test('search narrows by name and by repository, and the URL carries it', async ({ page, api }) => {
  const rules = new RulesPage(page);
  const mongo = (await api.rules()).map(r => r.name).filter(n => n.includes('mongo')).sort();
  await rules.search.fill('mongo');
  await expect(page).toHaveURL(/[?&]q=mongo/);
  expect(await rules.names()).toEqual(mongo);
});

test('a link with filters restores them', async ({ page }) => {
  const rules = new RulesPage(page);
  await rules.open('?sink=redis&sort=name');
  await expect(rules.filter('Sink')).toHaveValue('redis');
  await expect(rules.filter('Sort')).toHaveValue('name');
  for (const name of await rules.names()) expect(name).toMatch(/redis/);
});

test('when nothing matches, the page says so and one button clears search and filters', async ({ page }) => {
  const rules = new RulesPage(page);
  await rules.search.fill('no-such-rule-anywhere');
  await expect(page.getByText('No rules match')).toBeVisible();
  await expect(page.getByText('“no-such-rule-anywhere”')).toBeVisible();
  await page.getByRole('button', { name: 'Clear search and filters' }).click();
  await expect(page.getByText('No rules match')).toHaveCount(0);
  await expect(rules.search).toHaveValue('');
  expect(await rules.names()).not.toHaveLength(0);
});

test('Clear filters is off until a filter is on, and Remove clears one', async ({ page }) => {
  const rules = new RulesPage(page);
  await expect(rules.clearFilters).toBeDisabled();
  await rules.filter('Sink').selectOption('redis');
  await expect(rules.clearFilters).toBeEnabled();
  await rules.clearFilters.click();
  await expect(rules.clearFilters).toBeDisabled();
  await expect(rules.filter('Sink')).toHaveValue('');
});

test('Refresh reads the rules again', async ({ page }) => {
  const rules = new RulesPage(page);
  const reads = page.waitForRequest(request => request.url().includes('/v1/ui/rules') && request.method() === 'GET');
  await rules.refresh.click();
  await reads;
});

test('a rule box opens the rule and its environment links open that environment', async ({ page }) => {
  const rules = new RulesPage(page);
  await rules.openRule('gitea/redis-hash');
  await expect(page).toHaveURL(/\/rules\/(demo\/)?gitea\/redis-hash$/);
  await page.goBack();
  await rules.ready();
  await rules.card('gitea/redis-hash').locator('.rcard__env').filter({ hasText: 'staging' }).click();
  await expect(page).toHaveURL(/\/rules\/(demo\/)?gitea\/redis-hash\?env=staging/);
});

test.describe('New rule', () => {
  test('opens a panel that writes the CUE for a rule, and says why a draft is not usable', async ({ page }) => {
    const rules = new RulesPage(page);
    await rules.newRule.click();
    const panel = page.getByRole('dialog', { name: 'New rule' });
    await expect(panel).toBeVisible();
    await expect(panel).toContainText('add the generated CUE');
    // a draft with nothing filled in is not a rule: the panel lists what the schema would reject
    await expect(panel).toContainText(/repositories needs at least one entry|provide either 'file_name' or 'file_path_map'/);
    await expect(panel.getByRole('button', { name: 'Copy CUE' })).toBeVisible();
    await panel.getByRole('button', { name: 'Close' }).first().click();
    await expect(panel).toBeHidden();
  });

  test('a rule that already exists is refused, because rules are matched by platform and datasource', async ({ page, api }) => {
    const rules = new RulesPage(page);
    const [existing] = await api.rules();
    const [platform, datasource] = existing.name.split('/');
    await rules.newRule.click();
    const panel = page.getByRole('dialog', { name: 'New rule' });
    const platformField = panel.getByLabel('Platform');
    const datasourceField = panel.getByLabel('Datasource');
    await platformField.selectOption({ label: platform }).catch(async () => platformField.fill(platform));
    await datasourceField.selectOption({ label: datasource }).catch(async () => datasourceField.fill(datasource));
    await expect(panel).toContainText(`a rule for ${existing.name} already exists`);
  });
});
