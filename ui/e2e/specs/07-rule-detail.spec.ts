import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, as, PEOPLE } from '../fixtures';
import { SCHEMA_REPO, CONFIG_REPO } from '../fixtures/gitea';
import { RulePage } from '../pages/RulePage';

/**
 * One rule: its status, its environments, the tabs of the Rule panel, and the keys and diff tabs of the Environments
 * panel. covers: route:/rules/:platform/:datasource, route:/rules/:tenant/:platform/:datasource, action:rule.refresh,
 * action:rule.tab.overview, action:rule.tab.history, action:rule.tab.events, action:rule.tab.definition,
 * action:envtab.environments, action:envtab.keys, action:envtab.diff, action:keys.filter, action:keys.search, action:keys.env
 */
test.use(as('reader'));

const RULE = 'gitea/redis-hash';

test('the environments the page shows are the environments the API lists, each with its status', async ({ page, api }) => {
  const apiRule = await api.rule(RULE);
  const rule = new RulePage(page);
  await rule.open(RULE);
  expect(await rule.envNames()).toEqual(apiRule.targets.map(t => t.env).sort());
  await expect(page.getByRole('tab', { name: /Environments/ }).first()).toContainText(String(apiRule.targets.length));
});

test('each environment sets the state in Git beside the state in the sink', async ({ page, api }) => {
  const rule = new RulePage(page);
  await rule.open(RULE);
  const target = (await api.rule(RULE)).targets.find(t => t.env === 'production')!;
  const box = rule.env(target.env);
  await expect(box.getByLabel('Desired state, in Git')).toContainText('vartrack-demo/demo-configs@main');
  await expect(box.getByLabel('Current state, in Redis')).toContainText('ui-redis-hash-production');
});

test('the rule is reachable with the tenant in the path, and without when only one tenant has it', async ({ page }) => {
  const rule = new RulePage(page);
  await page.goto(`/rules/demo/${RULE}`);
  await rule.ready();
  await expect(page.getByText('Tenant: demo')).toBeVisible();
  await rule.open(RULE);
  await expect(page.getByText('Tenant: demo')).toBeVisible();
});

test('Refresh reads the rule again', async ({ page }) => {
  const rule = new RulePage(page);
  await rule.open(RULE);
  const read = page.waitForRequest(r => r.url().includes(`/v1/ui/rules/${RULE}`) && r.method() === 'GET');
  await rule.refresh.click();
  await read;
});

test.describe('the Rule panel', () => {
  test('Overview says how an environment is resolved: the branch_map of this rule', async ({ page }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.getByRole('tab', { name: 'Overview' }).click();
    const panel = page.locator('#rule-panel');
    await expect(panel).toContainText('develop');
    await expect(panel).toContainText('staging');
    await expect(panel).toContainText('production');
  });

  test('History lists the syncs of this rule, newest first, each linking to its task', async ({ page, api }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.getByRole('tab', { name: 'History' }).click();
    const timeline = page.getByRole('list', { name: 'Sync history, newest first' });
    await expect(timeline).toBeVisible();
    const [newest] = (await api.tasks()).filter(t => (t as unknown as { rule: string }).rule === RULE);
    expect(newest, 'the rule has been synced').toBeDefined();
    await expect(timeline.getByRole('link').first()).toBeVisible();
  });

  test('Events lists what happened to this rule, or says there were none', async ({ page }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.getByRole('tab', { name: 'Events' }).click();
    await expect(page.locator('#rule-panel').getByText(/No events for this rule|synced|Sync|drift/i).first()).toBeVisible();
  });

  test('Definition shows the rule as CUE, and copies it', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.getByRole('tab', { name: 'Definition' }).click();
    const panel = page.locator('#rule-panel');
    await expect(panel).toContainText('redis-hash');
    await expect(panel).toContainText('branch_map');
    await panel.getByRole('button', { name: 'Copy CUE' }).click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied).toContain('redis-hash');
  });
});

test.describe('the Environments panel', () => {
  test('Keys lists the keys of an environment next to Git and the sink, and filters them', async ({ page, api }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.getByRole('tab', { name: 'Keys' }).click();
    const keys = (await api.keys(RULE, 'staging')).keys;
    expect(keys.length).toBeGreaterThan(5);
    const panel = page.locator('#environments-panel');
    await panel.getByRole('button', { name: 'staging' }).click();
    await expect(panel.getByText(keys[0].key, { exact: true })).toBeVisible();

    await panel.getByPlaceholder('Search keys and values…').fill('rate_limit');
    await expect(panel.getByText('api.rate_limit', { exact: true })).toBeVisible();
    await expect(panel.getByText('api.timeout_ms', { exact: true })).toHaveCount(0);
  });

  test('Keys marks a secret and masks its value', async ({ page, api }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.getByRole('tab', { name: 'Keys' }).click();
    const secret = (await api.keys(RULE, 'staging')).keys.find(k => k.secret);
    expect(secret, 'the settings file has a secret key').toBeDefined();
    expect(secret?.gitValue).toBe('***');
    const panel = page.locator('#environments-panel');
    await panel.getByRole('button', { name: 'staging' }).click();
    await panel.getByPlaceholder('Search keys and values…').fill(secret!.key);
    const row = panel.getByRole('row').filter({ hasText: secret!.key });
    await expect(row).toContainText('***');
    await expect(row.getByLabel(/Secret — value resolved from the secret manager/)).toBeVisible();
  });

  test('Keys can show only the changed, the secret or the protected ones', async ({ page }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.getByRole('tab', { name: 'Keys' }).click();
    const panel = page.locator('#environments-panel');
    await panel.getByRole('button', { name: 'Secrets' }).click();
    await expect(panel.getByText('api.secret_key', { exact: true })).toBeVisible();
    await expect(panel.getByText('api.rate_limit', { exact: true })).toHaveCount(0);
    await panel.getByRole('button', { name: /^Changed/ }).click();
    await expect(page).toHaveURL(/keys=changed/);
  });

  test('Diff compares the sink with Git for every environment of the rule', async ({ page }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.getByRole('tab', { name: 'Diff' }).click();
    await expect(page).toHaveURL(/tab=diff/);
    await expect(page.locator('#environments-panel')).toContainText(/No differences|current|desired/i);
  });

  test('the Diff action of the action bar opens the same tab', async ({ page }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await page.locator('.action-bar').getByRole('button', { name: 'Diff' }).click();
    await expect(page).toHaveURL(/tab=diff/);
  });
});

test.describe('an environment no branch feeds any more', () => {
  test.use(as('admin'));

  test('is shown as no longer synced, does not count against the rule, and an administrator can forget it', async ({ page, browser, baseURL, adminApi, gitea, stack }) => {
    const world = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'world', 'bundle.json'), 'utf8');
    const rules = JSON.parse(world) as Array<{ datasource: string; branch_map?: Record<string, string> }>;
    const changed = JSON.parse(world) as typeof rules;
    delete changed.find(r => r.datasource === 'redis-hash')!.branch_map!.develop;   // develop no longer feeds staging

    const rule = new RulePage(page);
    try {
      await gitea.put(SCHEMA_REPO, 'bundle.json', JSON.stringify(changed, null, 2), 'ui-e2e: develop no longer feeds staging');
      await stack.warm();

      await rule.open(RULE);
      const staging = rule.env('staging');
      await expect(staging).toContainText('No longer synced by this rule');
      await expect(staging).toContainText('nothing feeds staging');
      await expect(staging.getByRole('button', { name: 'Sync staging' })).toBeDisabled();
      // it is not compared with the rule: its sink is not read, and it is not drift
      expect((await adminApi.rule(RULE)).targets.find(t => t.env === 'staging')).toMatchObject({ drift: 'not_watched' });
      await expect(rule.env('production')).toHaveAccessibleName(/production: Synced/);

      // only an administrator may forget it
      const reader = await browser.newContext({ baseURL, storageState: PEOPLE.reader.storageState });
      try {
        const readerPage = await reader.newPage();
        const readerRule = new RulePage(readerPage);
        await readerRule.open(RULE);
        await expect(readerRule.env('staging')).toContainText('No longer synced by this rule');
        await expect(readerRule.env('staging').getByRole('button', { name: 'Forget staging' })).toHaveCount(0);
      } finally {
        await reader.close();
      }

      await staging.getByRole('button', { name: 'Forget staging' }).click();
      await expect(page.getByRole('status').filter({ hasText: 'staging forgotten' })).toBeVisible();
      await expect(rule.env('staging')).toHaveCount(0);
      expect((await adminApi.rule(RULE)).targets.map(t => t.env)).not.toContain('staging');
    } finally {
      // put the world back: the rule as it was, and staging written again by a push to develop
      await gitea.put(SCHEMA_REPO, 'bundle.json', world, 'ui-e2e: the rules, as they were');
      await stack.warm();
      await adminApi.succeeded(await stack.push('redis-hash', 'develop', await gitea.head(CONFIG_REPO, 'develop')), 90_000);
    }
  });
});

