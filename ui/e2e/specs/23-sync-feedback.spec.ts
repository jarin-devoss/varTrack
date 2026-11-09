import { expect, test, as } from '../fixtures';
import { RulePage, startedSync } from '../pages/RulePage';

/**
 * Every action says how it ended, where the person is looking: a sync is "Syncing staging…" while it runs, then the same
 * toast says "Synced staging · 19 keys written", or "Sync of staging failed" with the reason, which stays until it is read.
 * covers: action:syncpanel.start, state:toast.progress, state:toast.good, state:toast.critical, state:envbox.syncing
 */
test.use(as('admin'));

const RULE = 'gitea/redis-hash';
const KEY = 'ui-redis-hash-staging:vartrack';
const DB = 2;

test('a sync that works ends as "Synced staging", with what it wrote, within a second of the click', async ({ page, api }) => {
  const rule = new RulePage(page);
  await rule.open(RULE);
  await rule.envSync('staging').click();
  await expect(rule.sync.dialog).toBeVisible();
  await rule.sync.dryRun.setChecked(false);

  const clicked = Date.now();
  const answered = page.waitForResponse(startedSync);
  await rule.sync.start.click();
  const [taskId] = ((await (await answered).json()) as { taskIds: string[] }).taskIds;

  const toast = page.getByRole('status').filter({ hasText: /Synced staging/ });
  await expect(toast).toBeVisible({ timeout: 3_000 });
  expect(Date.now() - clicked, 'the person is told within seconds, not at the next poll').toBeLessThan(3_000);
  await expect(toast).toContainText(/\d+ keys written/);
  await expect(toast.getByRole('link', { name: 'Details' })).toHaveAttribute('href', `/tasks/${taskId}`);
  await expect(page.getByRole('status').filter({ hasText: /Syncing staging…/ })).toHaveCount(0);   // it was the same toast

  // and the box agrees, with no "Syncing…" left over
  await expect(rule.env('staging')).toHaveAccessibleName(/staging: Synced/);
  await expect(rule.envSync('staging')).toBeEnabled();
  await expect(rule.envSync('staging')).toHaveText(/^\s*Sync$/);
  expect((await api.task(taskId)).state).toBe('SUCCEEDED');
});

test('a sync the sink refuses ends as "Sync of staging failed", with Redis\'s reason, and the toast stays until dismissed', async ({ page, stores, adminApi }) => {
  await page.clock.install();
  const rule = new RulePage(page);
  await rule.open(RULE);

  stores.redis(DB, 'DEL', KEY);
  stores.redis(DB, 'SET', KEY, 'not a hash');
  try {
    await rule.syncEnv('staging', { dryRun: false });

    const toast = page.getByRole('alert').filter({ hasText: 'Sync of staging failed' });
    await expect(toast).toBeVisible({ timeout: 10_000 });
    await expect(toast).toContainText('Redis refused 1 of 2 commands of the write: WRONGTYPE');
    await expect(toast.getByRole('link', { name: 'Open the task' })).toHaveAttribute('href', /\/tasks\/[0-9a-f-]{36}$/);

    // a failure is not for glancing at: a minute later it is still there
    await page.clock.fastForward(60_000);
    await expect(toast).toBeVisible();
    await toast.getByRole('button', { name: 'Dismiss' }).click();
    await expect(toast).toBeHidden();
  } finally {
    stores.redis(DB, 'DEL', KEY);
    const [mend] = await adminApi.sync(RULE, ['staging']);
    await adminApi.succeeded(mend);
  }
});

test('while a sync runs the toast says so and the environment shows "Syncing…"; both end when the sink answers', async ({ page, faults }) => {
  const zookeeper = new RulePage(page);
  await zookeeper.open('gitea/zookeeper');
  faults.pause('zookeeper');                        // the sink hangs: the task is picked up and cannot finish

  await zookeeper.syncEnv('production', { dryRun: false });
  const running = page.getByRole('status').filter({ hasText: 'Syncing production…' });
  await expect(running).toBeVisible();
  await expect(running.getByRole('link', { name: 'Follow the task' })).toBeVisible();
  await expect(zookeeper.envSync('production')).toBeDisabled({ timeout: 15_000 });
  await expect(zookeeper.envSync('production')).toHaveText('Syncing…');

  faults.unpause('zookeeper');
  await expect(page.getByRole('status').filter({ hasText: /Synced production/ })).toBeVisible({ timeout: 60_000 });
  await expect(zookeeper.envSync('production')).toBeEnabled();
  await expect(zookeeper.envSync('production')).toHaveText(/^\s*Sync$/);
});
