import { expect, test, as } from '../fixtures';
import { SettingsPage } from '../pages/SettingsPage';

/**
 * Settings: the connection to the services, their health, what the bundle holds, and the look of the UI. Preferences stay
 * in the browser: each spec has its own browser, so nothing a spec saves reaches another.
 * covers: route:/settings, route:/settings/:section, action:settings.gateway, action:settings.orchestrator, action:settings.watcher,
 * action:settings.test, action:settings.save, action:settings.discard, action:settings.endpoints, action:settings.fold,
 * action:settings.theme, action:settings.live, action:settings.reset, state:settings.connection-dirty, state:settings.service-down
 */
test.use(as('reader'));

test('the page names its five sections, and each one folds', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  for (const title of ['Connection', 'Services', 'Datasources', 'Platforms, secrets and schemas', 'Appearance']) {
    await expect(settings.panel(title)).toBeVisible();
  }
  await expect(settings.fold('Services')).toHaveAttribute('aria-expanded', 'true');
  await settings.fold('Services').click();
  await expect(settings.fold('Services')).toHaveAttribute('aria-expanded', 'false');
  await expect(settings.panel('Services').getByRole('table')).toHaveCount(0);
  await settings.fold('Services').click();
  await expect(settings.panel('Services').getByRole('table')).toBeVisible();
});

test('Services shows the three services ready', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  const services = settings.panel('Services');
  for (const name of ['gateway', 'orchestrator', 'watcher']) {
    await expect(services.getByRole('row').filter({ hasText: new RegExp(`^${name}`, 'i') })).toContainText('Ready');
  }
});

test('Datasources lists what config.cue defines, the sink behind each, and the rules that use it', async ({ page, api }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  const table = settings.panel('Datasources').getByRole('table');
  await expect(table).toBeVisible();
  const rules = await api.rules();
  const redisHash = table.getByRole('row').filter({ has: page.getByRole('cell', { name: 'redis-hash', exact: true }) });
  await expect(redisHash).toContainText('Redis');
  await expect(redisHash).toContainText('gitea/redis-hash');
  expect(rules.some(r => r.name === 'gitea/redis-hash')).toBe(true);
  await expect(settings.panel('Datasources')).toContainText('read-only');
  await expect(settings.panel('Datasources')).toContainText('demo');
});

test('Platforms, secrets and schemas names the Git platform, the secret manager and the schema repository', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  const panel = settings.panel('Platforms, secrets and schemas');
  await expect(panel.locator('div', { has: page.getByText('Platform', { exact: true }) }).first()).toContainText('gitea');
  await expect(panel).toContainText('Secret manager');
  await expect(panel).toContainText('vault');
  await expect(panel.locator('dd', { hasText: 'vartrack-demo/schemas@main' })).toContainText('gitea · tenant demo');
  await expect(panel).toContainText('Change it in Git.');
});

test('Test connection checks the three URLs as typed, without saving them', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  await expect(settings.save).toBeDisabled();

  await settings.orchestratorUrl.fill('http://127.0.0.1:9');
  await expect(settings.save).toBeEnabled();
  await expect(settings.discard).toBeVisible();
  await settings.test.click();
  const connection = settings.panel('Connection');
  await expect(connection.getByRole('row').filter({ hasText: /^orchestrator/i })).toContainText(/Unreachable|Not ready/);
  await expect(connection.getByRole('row').filter({ hasText: /^gateway/i })).toContainText('Ready');

  // nothing was saved: Discard puts the saved URL back
  await settings.discard.click();
  await expect(settings.orchestratorUrl).toHaveValue('/api/orchestrator');
  await expect(settings.save).toBeDisabled();
});

test('Save keeps a URL, and a UI pointed at nothing says so on every page and can be repaired from Settings', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  await settings.orchestratorUrl.fill('http://127.0.0.1:9');
  await settings.save.click();
  await expect(page.getByRole('status').filter({ hasText: 'Connection saved' })).toBeVisible();
  await expect(settings.save).toBeDisabled();

  // the choice outlives a reload
  await page.reload();
  await expect(settings.orchestratorUrl).toHaveValue('http://127.0.0.1:9');

  await page.goto('/rules');
  await expect(page.getByRole('button', { name: 'Try again' }).or(page.getByText(/Can't load|couldn't|doesn't provide/i)).first()).toBeVisible();

  await settings.open();
  await settings.orchestratorUrl.fill('/api/orchestrator');
  await settings.save.click();
  await page.goto('/rules');
  await expect(page.locator('article.rcard').first()).toBeVisible();
});

test('the endpoints list says which calls each screen makes and that the server has them', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  const details = settings.panel('Connection').locator('details');
  await details.getByText('Which endpoints each screen uses').click();
  const rows = details.getByRole('row');
  await expect(rows.filter({ hasText: 'POST /v1/cli/sync' })).toContainText('needs your Git token');
  await expect(rows.filter({ hasText: 'GET /v1/ui/events' })).toContainText('Available');
  expect(await details.getByText('Not implemented').count() + (await details.getByText('Returns 501').count())).toBe(0);
});

test('a link to a section opens the page at that section', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open('/settings/appearance');
  await expect(settings.panel('Appearance')).toBeInViewport();
  await settings.open('/settings#services');
  await expect(settings.panel('Services')).toBeInViewport();
});

test('Theme changes the look at once and is remembered', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  const html = page.locator('html');
  await expect(settings.theme.getByRole('button', { name: 'System' })).toHaveAttribute('aria-pressed', 'true');

  await settings.theme.getByRole('button', { name: 'Dark' }).click();
  await expect(html).toHaveAttribute('data-theme', 'dark');
  await expect(settings.theme.getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true');
  await page.reload();
  await expect(html).toHaveAttribute('data-theme', 'dark');

  await settings.theme.getByRole('button', { name: 'Light' }).click();
  await expect(html).toHaveAttribute('data-theme', 'light');
  await settings.theme.getByRole('button', { name: 'System' }).click();
  await expect(html).not.toHaveAttribute('data-theme', /.+/);
});

test('Live updates is the same switch as the one in the header', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  await expect(settings.live).toBeChecked();
  await settings.live.uncheck();
  await expect(page.getByRole('button', { name: 'Resume live updates' })).toBeVisible();
  await page.getByRole('button', { name: 'Resume live updates' }).click();
  await expect(settings.live).toBeChecked();
});

test('Reset puts the theme and the favorites back, and keeps the connection', async ({ page }) => {
  const settings = new SettingsPage(page);
  await settings.open();
  await settings.theme.getByRole('button', { name: 'Dark' }).click();
  await settings.live.uncheck();
  await settings.reset.click();
  await expect(page.getByRole('status').filter({ hasText: 'Favorites, theme and list settings reset' })).toBeVisible();
  await expect(settings.theme.getByRole('button', { name: 'System' })).toHaveAttribute('aria-pressed', 'true');
  await expect(settings.live).toBeChecked();
  await expect(settings.orchestratorUrl).toHaveValue('/api/orchestrator');
});
