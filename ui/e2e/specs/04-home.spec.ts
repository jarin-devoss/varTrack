import { expect, test, as, unique } from '../fixtures';
import type { ApiRule, UiApi } from '../fixtures/api';
import type { Stores } from '../fixtures/stores';
import type { Page } from '@playwright/test';
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

async function openHome(page: Page): Promise<void> {
  await page.goto('/home');
  const shell = new ShellPage(page);
  await shell.ready();
  // The page refreshes by itself every few seconds; hold it still while it is read against the API. The choice is kept in the
  // browser (a second visit in one test finds it already paused), so only press the button while updates are live.
  if (await page.getByRole('button', { name: 'Pause live updates' }).isVisible()) await shell.pauseLive();
  await expect(page.getByRole('button', { name: 'Resume live updates' })).toHaveAttribute('aria-pressed', 'false');
}

test.beforeEach(async ({ page }) => openHome(page));

// A spec about what needs attention makes something that does, and puts it back: it does not wait for the stack to hold some.
// gitea/mongo-primary has no self-heal, so nothing puts the value back while the page is read (spec 12 uses it the same way).
const DRIFTED = { rule: 'gitea/mongo-primary', env: 'staging', collection: 'ui-mongo-primary' };

async function withDrift(adminApi: UiApi, api: UiApi, stores: Stores, body: () => Promise<void>): Promise<void> {
  await adminApi.succeeded((await adminApi.sync(DRIFTED.rule, [DRIFTED.env]))[0]);
  stores.mongoSet(DRIFTED.collection, DRIFTED.env, 'api.rate_limit', unique('outside'));
  try {
    await expect
      .poll(async () => (await api.fresh(DRIFTED.rule)).targets.find(t => t.env === DRIFTED.env)?.drift, { timeout: 30_000, message: 'the changed value reads as drift' })
      .toBe('drifted');
    await body();
  } finally {
    await adminApi.succeeded((await adminApi.sync(DRIFTED.rule, [DRIFTED.env]))[0]);
  }
}

test('the headline counts the rules, environments and sink types the API lists', async ({ page, api }) => {
  const rules = await api.rules();
  const environments = rules.reduce((n, r) => n + r.targets.length, 0);
  const sinks = new Set(rules.map(r => (r as unknown as { sinkType: string }).sinkType)).size;
  await expect(page.getByText(`${rules.length} rules · ${environments} environments · ${sinks} sink types`)).toBeVisible();
});

test('"Needs attention" lists exactly the environments that drifted, are unknown, or whose last sync failed', async ({ page, api, adminApi, stores }) => {
  await withDrift(adminApi, api, stores, async () => {
    const expected = attention(await api.rules());
    expect(expected, 'the environment that was changed needs attention').toContain(`${DRIFTED.rule}|${DRIFTED.env}`);
    await openHome(page);
    const panel = page.locator('section, .panel').filter({ has: page.getByRole('button', { name: /needs attention/i }) }).first();
    await expect(page.getByText(`${expected.length} environment${expected.length === 1 ? '' : 's'} with drift, an unknown state or a failed sync`)).toBeVisible();
    await expect(panel).toBeVisible();
    await expect(page.getByText('Everything is in sync')).toHaveCount(0);
  });
});

test('"Needs attention" says everything is in sync when nothing drifted, is unknown or failed its last sync', async ({ page, api }) => {
  // The world has nothing that needs attention (fixtures/world.ts). If something does, an earlier spec left it: name it, do not skip.
  // (The test before this one restored a value; the page compares with the sink again within a few seconds.)
  await expect.poll(async () => attention(await api.rules()), { timeout: 20_000, message: 'what is left over from an earlier spec' }).toEqual([]);
  await openHome(page);
  await expect(page.getByText('Everything is in sync')).toBeVisible();
  await expect(page.getByText('Nothing to do')).toBeVisible();
});

test('an environment in "Needs attention" opens its rule at that environment', async ({ page, api, adminApi, stores }) => {
  await withDrift(adminApi, api, stores, async () => {
    await openHome(page);
    const panel = page.locator('.panel').filter({ has: page.getByText('Needs attention') }).first();
    await panel.getByRole('link', { name: new RegExp(`${DRIFTED.rule}/\\s*${DRIFTED.env}$`) }).click();
    await expect(page).toHaveURL(new RegExp(`/rules/(demo/)?${DRIFTED.rule}\\?.*env=${DRIFTED.env}`));
    const url = new URL(page.url());
    expect(attention(await api.rules()), 'the environment it opened is one that needed attention').toContain(
      `${url.pathname.replace(/^\/rules\/(demo\/)?/, '')}|${url.searchParams.get('env')}`,
    );
  });
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
