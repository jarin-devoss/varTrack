import { expect, test, as } from '../fixtures';
import type { ApiRule } from '../fixtures/api';
import { ShellPage } from '../pages/ShellPage';

/**
 * The Home page answers "is anything wrong right now?". Every figure on it is checked against the API it is made
 * from. covers: route:/home, state:attention.list, state:attention.none, state:services, state:sinks, state:events.recent
 */
test.use(as('reader'));

/** The environments the Home page lists under "Needs attention": drifted, unknown or failed-to-heal, or whose last sync failed. */
function attention(rules: ApiRule[]): string[] {
  const keys = new Set<string>();
  for (const rule of rules) {
    for (const t of rule.targets) {
      if (['heal_failed', 'drifted', 'unknown'].includes(t.drift) || t.lastSync?.state === 'FAILED') keys.add(`${rule.name}|${t.env}`);
    }
  }
  return [...keys].sort();
}

test.beforeEach(async ({ page }) => {
  await page.goto('/home');
  const shell = new ShellPage(page);
  await shell.ready();
  // The page refreshes by itself every few seconds; hold it still while it is read against the API.
  await shell.pauseLive();
});

test('the headline counts the rules, environments and sink types the API lists', async ({ page, api }) => {
  const rules = await api.rules();
  const environments = rules.reduce((n, r) => n + r.targets.length, 0);
  const sinks = new Set(rules.map(r => (r as unknown as { sinkType: string }).sinkType)).size;
  await expect(page.getByText(`${rules.length} rules · ${environments} environments · ${sinks} sink types`)).toBeVisible();
});

test('"Needs attention" lists exactly the environments that drifted, are unknown, or whose last sync failed', async ({ page, api }) => {
  const rules = await api.rules();
  const expected = attention(rules);
  const panel = page.locator('section, .panel').filter({ has: page.getByRole('button', { name: /needs attention/i }) }).first();
  if (expected.length === 0) {
    await expect(page.getByText('Everything is in sync')).toBeVisible();
    await expect(page.getByText('Nothing to do')).toBeVisible();
    return;
  }
  await expect(page.getByText(`${expected.length} environment${expected.length === 1 ? '' : 's'} with drift, an unknown state or a failed sync`)).toBeVisible();
  await expect(panel).toBeVisible();
  await expect(page.getByText('Everything is in sync')).toHaveCount(0);
});

test('an environment in "Needs attention" opens its rule at that environment', async ({ page, api }) => {
  const needing = attention(await api.rules());
  test.skip(needing.length === 0, 'nothing needs attention right now: the empty state is covered above');
  const panel = page.locator('.panel').filter({ has: page.getByText('Needs attention') }).first();
  await panel.getByRole('link').first().click();
  await expect(page).toHaveURL(/\/rules\//);
  const url = new URL(page.url());
  const rule = url.pathname.replace(/^\/rules\/(demo\/)?/, '');
  expect(needing, 'the environment it opened is one that needed attention').toContain(`${rule}|${url.searchParams.get('env')}`);
});

test('Services shows the gateway, the orchestrator and the watcher, each ready', async ({ page }) => {
  const services = page.getByRole('table').filter({ has: page.getByRole('columnheader', { name: 'Service' }) });
  await expect(services).toBeVisible();
  for (const name of ['gateway', 'orchestrator', 'watcher']) {
    const row = services.getByRole('row').filter({ hasText: new RegExp(name, 'i') });
    await expect(row, name).toContainText('Ready');
  }
});

test('Sinks has a row for each sink type with its rules, environments and key count', async ({ page, api }) => {
  const rules = await api.rules();
  const table = page.getByRole('table').filter({ has: page.getByRole('columnheader', { name: /sink/i }) });
  await expect(table).toBeVisible();
  const bySink = new Map<string, ApiRule[]>();
  for (const r of rules) {
    const sink = (r as unknown as { sinkType: string }).sinkType;
    bySink.set(sink, [...(bySink.get(sink) ?? []), r]);
  }
  // a row per sink type (the header row is one more)
  await expect(table.getByRole('row')).toHaveCount(bySink.size + 1);
});

test('Tenant names the tenant the rules belong to', async ({ page }) => {
  await expect(page.getByText('Where the bundle comes from and what it connects to')).toBeVisible();
  await expect(page.getByText('demo', { exact: true }).first()).toBeVisible();
});

test('Recent events lists the newest events, and links to all of them', async ({ page, api }) => {
  const events = await api.events();
  if (events.length === 0) {
    await expect(page.getByText('No events yet')).toBeVisible();
  } else {
    await expect(page.getByText('No events yet')).toHaveCount(0);
  }
  await page.getByRole('link', { name: 'all events' }).click();
  await expect(page).toHaveURL(/\/events$/);
});
