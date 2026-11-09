import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, as, unique } from '../fixtures';
import { SCHEMA_REPO } from '../fixtures/gitea';
import { EnvironmentsPage } from '../pages/EnvironmentsPage';
import { RulePage } from '../pages/RulePage';

/**
 * Each environment has its own Diff, and there is no shared one. Every environment box has a Diff button (not only the one
 * that is out of sync); it is disabled, with the reason, when there is nothing to compare: an environment the rule produces and
 * has never written, and one no branch feeds any more (a leftover), whose keys are not compared with Git at all.
 * covers: state:diff.per-environment, state:diff.disabled, label:Last written by varTrack
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const world = fs.readFileSync(path.join(here, '..', 'world', 'bundle.json'), 'utf8');
const RULE = 'gitea/redis-string';
const KV = 'gitea/mongo-kv';
const COLLECTION = 'ui-diff-leftover';
const NOT_YET = /Not synced yet: nothing has been written to compare with Git/;
const LEFTOVER = /No longer synced by this rule: no branch feeds it, so there is nothing in Git to compare with/;

test.describe('as the reader', () => {
  test.use(as('reader'));

  test('every environment box has a Diff, not only the one that is out of sync, and it opens that environment on its Diff view', async ({ page, api, adminApi }) => {
    // develop and main are written (and match Git): synced environments have a Diff too
    for (const env of ['develop', 'main']) await adminApi.succeeded((await adminApi.sync(RULE, [env]))[0]);
    const rule = new RulePage(page);
    await rule.open(RULE);

    const view = await api.rule(RULE);
    expect(view.targets.map(t => t.env)).toEqual(expect.arrayContaining(['develop', 'main']));
    for (const t of view.targets) {
      await expect(rule.envDiff(t.env), `Diff of ${t.env}`).toBeVisible();
      if (t.notFed || t.drift === 'missing') await expect(rule.envDiff(t.env)).toBeDisabled();
      else await expect(rule.envDiff(t.env)).toBeEnabled();
    }
    await expect(rule.env('develop')).toContainText(/Synced|OutOfSync/);

    await rule.envDiff('develop').click();
    await expect(page).toHaveURL(/[?&]env=develop&view=diff/);
    await expect(page.getByRole('tablist', { name: 'Environment', exact: true }).getByRole('tab', { name: 'Diff' })).toHaveAttribute('aria-selected', 'true');
  });

  test('there is no rule-wide Diff: not in the action bar, not a tab of the Environments panel, not on the rule card, and ?tab=diff opens Environments', async ({ page }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await expect(page.locator('.action-bar').getByRole('button', { name: 'Diff', exact: true })).toHaveCount(0);
    const tabs = page.getByRole('tablist', { name: 'Environments', exact: true }).getByRole('tab');
    await expect(tabs).toHaveText([/^Environments/, /^Keys$/]);

    await page.goto(`/rules/${RULE}?tab=diff`);
    await expect(page.getByRole('tablist', { name: 'Environments', exact: true }).getByRole('tab', { name: /^Environments/ })).toHaveAttribute('aria-selected', 'true');

    await page.goto('/rules?q=redis-string');
    const card = page.getByRole('article', { name: new RegExp(`^${RULE}:`) });
    await expect(card).toBeVisible();
    await expect(card.getByRole('link', { name: /Diff/ })).toHaveCount(0);
    await expect(card.getByRole('button', { name: /Diff/ })).toHaveCount(0);
  });
});

test.describe('as admin', () => {
  test.use(as('admin'));

  /** The browser suite's world with mongo-kv given a file, a destination and these environments. */
  const bundle = (branchMap: Record<string, string>): string => {
    const rules = JSON.parse(world) as Array<Record<string, unknown>>;
    Object.assign(rules.find(r => r.datasource === 'mongo-kv')!, {
      file_name: 'app-settings.yaml',
      branch_map: branchMap,
      destination_template: COLLECTION,
    });
    return JSON.stringify(rules, null, 2);
  };

  test('an environment never written has a Diff that is off and says so; one no branch feeds any more is off too, is not compared with Git, and is not counted', async ({
    page, gitea, stack, adminApi, stores,
  }) => {
    const fresh = unique('first').toLowerCase();
    const leftover = unique('left').toLowerCase();
    const rule = new RulePage(page);
    try {
      // 1. produced, never written
      await gitea.put(SCHEMA_REPO, 'bundle.json', bundle({ main: leftover, develop: fresh }), 'ui-e2e: two environments', 'main');
      await stack.warm();
      await rule.open(KV);
      await expect(rule.envDiff(fresh)).toBeDisabled();
      await expect(rule.envDiff(fresh)).toHaveAccessibleDescription(NOT_YET);
      await expect(rule.envDiff(fresh)).toHaveAttribute('title', NOT_YET);
      // a link to its diff view opens its summary: there is nothing behind the other views
      await page.goto(`/rules/${KV}?env=${fresh}&view=diff`);
      const views = page.getByRole('tablist', { name: 'Environment', exact: true }).getByRole('tab');
      await expect(views).toHaveText(['Summary', 'History']);
      await expect(page.getByRole('tab', { name: 'Summary' })).toHaveAttribute('aria-selected', 'true');

      // 2. written, then no branch feeds it any more
      await adminApi.succeeded((await adminApi.sync(KV, [leftover]))[0]);
      await rule.open(KV);
      await expect(rule.envDiff(leftover)).toBeEnabled();
      await gitea.put(SCHEMA_REPO, 'bundle.json', bundle({ develop: fresh }), 'ui-e2e: one environment left', 'main');
      await stack.warm();
      await rule.open(KV);
      await expect(rule.env(leftover)).toContainText('No longer synced by this rule');
      await expect(rule.envDiff(leftover)).toBeDisabled();
      await expect(rule.envDiff(leftover)).toHaveAccessibleDescription(LEFTOVER);

      // its panel: no Diff, Current or Desired; what varTrack last wrote is not called what Git says
      await rule.envDetails(leftover).click();
      await expect(page.getByRole('tablist', { name: 'Environment', exact: true }).getByRole('tab')).toHaveText(['Summary', 'History']);
      const panel = page.getByRole('dialog');
      await expect(panel.getByLabel('Last written by varTrack')).toBeVisible();
      await expect(panel.getByLabel('Desired state, in Git')).toHaveCount(0);
      await page.goto(`/rules/${KV}?env=${leftover}&view=diff`);
      await expect(page.getByRole('tab', { name: 'Summary' })).toHaveAttribute('aria-selected', 'true');

      // the keys are not compared with Git: the API says why
      const keys = await page.request.get(`/api/orchestrator/v1/ui/rules/${KV}/targets/${leftover}/keys`);
      expect(keys.status()).toBe(409);
      expect(((await keys.json()) as { detail: string }).detail).toMatch(new RegExp(`^nothing feeds ${leftover} in the mongo-kv rule`));

      // the rule's status counts what it produces, and names the leftover after it
      await rule.open(KV);
      const status = page.locator('.statgrid');
      await expect(status).toContainText(/\d+ no longer synced: .*/);
      await expect(status).toContainText(leftover);
      await expect(status).toContainText(/of 1 environment match Git/);

      // the Environments page row says the same
      const envs = new EnvironmentsPage(page);
      await envs.open('?q=mongo-kv');
      await envs.toggle(KV, leftover).click();
      const row = envs.row(KV, leftover);
      await expect(row.getByRole('button', { name: 'Diff' })).toBeDisabled();
      await expect(row.getByRole('button', { name: 'Diff' })).toHaveAttribute('title', LEFTOVER);
      await expect(row.getByRole('link', { name: 'Diff' })).toHaveCount(0);
    } finally {
      await gitea.put(SCHEMA_REPO, 'bundle.json', world, 'ui-e2e: the rules, as they were', 'main');
      await stack.warm();
      stores.mongo(`db.getCollection(${JSON.stringify(COLLECTION)}).deleteMany({ _vt_env: ${JSON.stringify(leftover)} })`);
    }
    // an administrator drops the leftover from the record
    await rule.open(KV);
    await rule.env(leftover).getByRole('button', { name: `Forget ${leftover}` }).click();
    await expect(rule.env(leftover)).toHaveCount(0);
  });
});

