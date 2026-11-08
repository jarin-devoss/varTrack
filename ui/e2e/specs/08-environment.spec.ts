import { expect, test, as, unique } from '../fixtures';
import { RulePage } from '../pages/RulePage';

/**
 * One environment, opened from its rule: Summary, Diff, Current, Desired, History. The sink is changed directly where
 * a spec needs a difference between it and Git; it is put back after.
 * covers: action:env.open, action:env.view.summary, action:env.view.diff, action:env.view.current, action:env.view.desired,
 * action:env.view.history, state:key.synced, state:key.changed, state:key.ignored, state:secret.masked, state:diff.none
 */
test.use(as('reader'));

const RULE = 'gitea/redis-hash';
const ENV = 'staging';
const HASH = 'ui-redis-hash-staging:vartrack';
const DB = 2;

/** Open the environment's panel on a view; the URL carries both, so a link shares it. */
async function openEnv(page: import('@playwright/test').Page, view = 'summary') {
  const rule = new RulePage(page);
  await page.goto(`/rules/${RULE}?env=${ENV}${view === 'summary' ? '' : `&view=${view}`}`);
  await rule.ready();
  const panel = page.getByRole('dialog').filter({ has: page.getByRole('tablist', { name: 'Environment', exact: true }) });
  await expect(panel).toBeVisible();
  return panel;
}

/** The row of one key in the Current or Desired table. */
const row = (panel: import('@playwright/test').Locator, key: string) => panel.getByRole('row').filter({ has: panel.page().getByText(key, { exact: true }) });

test('opening an environment shows its summary: how it was resolved, the self-heal state and the last sync', async ({ page, api }) => {
  const rule = new RulePage(page);
  await rule.open(RULE);
  await rule.envDetails(ENV).click();
  await expect(page).toHaveURL(/[?&]env=staging/);
  const panel = page.getByRole('dialog').filter({ has: page.getByRole('tablist', { name: 'Environment', exact: true }) });
  await expect(panel).toBeVisible();

  const target = (await api.rule(RULE)).targets.find(t => t.env === ENV)!;
  await expect(panel.getByText('Environment resolved by')).toBeVisible();
  await expect(panel.getByText('Self-heal', { exact: true })).toBeVisible();
  await expect(panel.getByText('Last sync', { exact: true })).toBeVisible();
  await expect(panel.getByRole('link', { name: target.lastSync!.taskId.slice(0, 8) })).toBeVisible();
  await expect(panel).toContainText(`${target.keyCount} keys`);
});

test('Escape closes the panel and the URL forgets the environment', async ({ page }) => {
  const panel = await openEnv(page);
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
  await expect(page).not.toHaveURL(/env=/);
});

test('the five views are tabs, and each is a link', async ({ page }) => {
  const panel = await openEnv(page);
  for (const [tab, param] of [['Diff', 'diff'], ['Current', 'current'], ['Desired', 'desired'], ['History', 'history']] as const) {
    await panel.getByRole('tab', { name: tab }).click();
    await expect(page).toHaveURL(new RegExp(`view=${param}`));
    await expect(panel.getByRole('tab', { name: tab })).toHaveAttribute('aria-selected', 'true');
  }
  await panel.getByRole('tab', { name: 'Summary' }).click();
  await expect(page).not.toHaveURL(/view=/);
});

test('Current and Desired list every key, and a secret is masked in both', async ({ page, api }) => {
  const keys = (await api.keys(RULE, ENV)).keys;
  const panel = await openEnv(page, 'current');
  await expect(panel).toContainText('ui-redis-hash-staging');
  for (const key of keys.slice(0, 5)) await expect(row(panel, key.key)).toContainText(String(key.liveValue));
  const secret = keys.find(k => k.secret)!;
  await expect(row(panel, secret.key)).toContainText('***');

  await panel.getByRole('tab', { name: 'Desired' }).click();
  await expect(panel).toContainText('What Git says should be there');
  await expect(row(panel, secret.key)).toContainText('***');
  for (const key of keys.slice(0, 5)) await expect(row(panel, key.key)).toContainText(String(key.gitValue));
});

test('Diff says "No differences" when the sink matches Git', async ({ page, api }) => {
  const keys = (await api.keys(RULE, ENV)).keys;
  expect(keys.every(k => k.state === 'synced' || k.state === 'ignored'), 'the sink matches Git before the check').toBe(true);
  const panel = await openEnv(page, 'diff');
  await expect(panel.getByText('No differences')).toBeVisible();
});

test.describe('with a value changed directly in the sink', () => {
  const FIELD = 'api.rate_limit';
  let original = '';

  test.beforeEach(async ({ stores }) => {
    original = stores.redisGet(DB, HASH, FIELD);
    expect(original, 'the sink holds the key').not.toBe('');
  });

  test.afterEach(async ({ stores }) => {
    stores.redisSet(DB, HASH, original, FIELD);
  });

  test('Diff puts the sink beside Git for the key, and Inline shows it as minus and plus', async ({ page, api, stores }) => {
    const tampered = unique('tampered');
    stores.redisSet(DB, HASH, tampered, FIELD);
    await expect.poll(async () => (await api.keys(RULE, ENV)).keys.find(k => k.key === FIELD)?.state, { timeout: 20_000 }).toBe('drifted');

    const panel = await openEnv(page, 'diff');
    await expect(panel.getByText('No differences')).toHaveCount(0);
    await expect(panel.getByText('1 changed')).toBeVisible();
    // side by side: the key, what the sink holds, what Git says
    await expect(panel.getByRole('columnheader', { name: 'Current · sink' })).toBeVisible();
    await expect(panel.getByRole('columnheader', { name: 'Desired · Git' })).toBeVisible();
    const split = panel.getByRole('row').filter({ hasText: FIELD });
    await expect(split.getByRole('cell').nth(1)).toHaveText(tampered);
    await expect(split.getByRole('cell').nth(2)).toHaveText(original);

    // inline: minus is the sink, plus is Git
    await panel.getByRole('checkbox', { name: 'Inline' }).check();
    const inline = panel.getByRole('table', { name: /Inline diff/ });
    await expect(inline).toBeVisible();
    await expect(inline.getByRole('row').filter({ has: page.getByLabel('current') })).toContainText(tampered);
    await expect(inline.getByRole('row').filter({ has: page.getByLabel('desired') })).toContainText(original);
    await panel.getByRole('checkbox', { name: 'Inline' }).uncheck();
  });

  test('Current shows the value the sink holds now, Desired the one Git has', async ({ page, api, stores }) => {
    const tampered = unique('now');
    stores.redisSet(DB, HASH, tampered, FIELD);
    await expect.poll(async () => (await api.keys(RULE, ENV)).keys.find(k => k.key === FIELD)?.liveValue, { timeout: 20_000 }).toBe(tampered);

    const panel = await openEnv(page, 'current');
    await expect(row(panel, FIELD)).toContainText(tampered);
    await panel.getByRole('tab', { name: 'Desired' }).click();
    await expect(row(panel, FIELD)).toContainText(original);
  });

  test('a key matching drift_ignore is shown as ignored, and is not drift', async ({ page, api, stores }) => {
    const IGNORED = 'features.dark_mode';
    const before = stores.redisGet(DB, HASH, IGNORED);
    try {
      stores.redisSet(DB, HASH, unique('flag'), IGNORED);
      await expect.poll(async () => (await api.keys(RULE, ENV)).keys.find(k => k.key === IGNORED)?.state, { timeout: 20_000 }).toBe('ignored');
      const rule = new RulePage(page);
      await rule.open(RULE);
      await page.getByRole('tab', { name: 'Keys' }).click();
      const panel = page.locator('#environments-panel');
      await panel.getByRole('group', { name: 'Environment' }).getByRole('button', { name: ENV }).click();
      await panel.getByPlaceholder('Search keys and values…').fill(IGNORED);
      await expect(panel.getByRole('row').filter({ hasText: IGNORED })).toContainText('Not compared');
    } finally {
      stores.redisSet(DB, HASH, before, IGNORED);
    }
  });
});

test('History lists the syncs of this environment, newest first', async ({ page, api }) => {
  const panel = await openEnv(page, 'history');
  const target = (await api.rule(RULE)).targets.find(t => t.env === ENV)!;
  await expect(panel.getByRole('list', { name: 'Sync history, newest first' })).toBeVisible();
  await expect(panel.getByRole('link', { name: new RegExp(target.lastSync!.taskId.slice(0, 8)) }).first()).toBeVisible();
});

test('a link to an environment the rule does not have opens the rule without a panel', async ({ page }) => {
  const rule = new RulePage(page);
  await page.goto(`/rules/${RULE}?env=no-such-environment`);
  await rule.ready();
  await expect(page.getByRole('tablist', { name: 'Environment', exact: true })).toHaveCount(0);
});
