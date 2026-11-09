import { expect, test, as, unique } from '../fixtures';
import { CONFIG_REPO } from '../fixtures/gitea';
import { EnvironmentsPage } from '../pages/EnvironmentsPage';
import { RulePage, startedSync } from '../pages/RulePage';

/**
 * A dry run is not a sync, and shows its log where it was started. Someone who may only dry-run (the reader) has Sync
 * disabled, with the reason, and Dry run as its own button; a dry run started from the Sync panel keeps the panel open and
 * shows what was read, how the schema judged it, and what a sync would add, change and delete; and the real sync of what
 * was checked is offered from there to whoever may sync. An earlier dry run's log reopens over the rule's page.
 * covers: state:sync.disabled-read-only, state:dryrun.log, state:dryrun.would-change, action:dryrun.back, action:dryrun.again,
 * action:dryrun.sync-these, action:history.show-log, state:rule.no-file
 */
const RULE = 'gitea/redis-string'; // strict_validation; its develop environment reads app-settings.yaml on develop
const ENV = 'develop';
const DB = 3;
const PREFIX = 'ui-redis-string-develop:';
const REASON = /You can read this rule's repository but not write to it/;

test.describe('as the reader', () => {
  test.use(as('reader'));

  /** The sink as Git says, then two keys changed outside Git: one deleted, one given another value. */
  async function drift(adminApi: { sync: Function; succeeded: Function }, stores: { redisDelete: Function; redisGet: Function; redisSet: Function }) {
    await adminApi.succeeded((await adminApi.sync(RULE, [ENV]))[0]);
    const level = stores.redisGet(DB, `${PREFIX}observability_log_level`) as string;
    stores.redisDelete(DB, `${PREFIX}app_name`);
    stores.redisSet(DB, `${PREFIX}observability_log_level`, unique('outside'));
    return level;
  }

  test('every Sync is disabled and says why, and Dry run is its own button: on the card, the rule page, an environment row and the Keys tab', async ({ page, adminApi, stores, stack }) => {
    await drift(adminApi, stores);
    await stack.warm();
    try {
      // the card
      await page.goto(`/rules?q=redis-string`);
      const card = page.getByRole('article', { name: new RegExp(`^${RULE}:`) });
      await expect(card.getByRole('button', { name: 'Sync', exact: true })).toBeDisabled();
      await expect(card.getByRole('button', { name: 'Sync', exact: true })).toHaveAccessibleDescription(REASON);
      await expect(card.getByRole('button', { name: 'Dry run', exact: true })).toBeEnabled();

      // the rule page: the bar, and the environment's box
      const rule = new RulePage(page);
      await rule.open(RULE);
      await expect(rule.syncAll).toBeDisabled();
      await expect(rule.syncAll).toHaveAccessibleDescription(REASON);
      await expect(rule.dryRunAll).toBeEnabled();
      await expect(page.getByText(REASON).first()).toBeVisible();
      await expect(rule.envSync(ENV)).toBeDisabled();
      await expect(rule.envSync(ENV)).toHaveAccessibleDescription(REASON);
      await expect(rule.envDryRun(ENV)).toBeEnabled();

      // the Keys tab offers a Sync of the changed keys: off, with a Dry run beside it
      await page.getByRole('tab', { name: 'Keys' }).click();
      await expect(page.getByRole('button', { name: `Sync ${ENV}` })).toBeDisabled();
      await expect(page.getByRole('button', { name: `Dry run ${ENV}` })).toBeEnabled();

      // an environment's row
      const envs = new EnvironmentsPage(page);
      await envs.open('?q=redis-string');
      await envs.toggle(RULE, ENV).click();
      const row = envs.row(RULE, ENV);
      await expect(row.getByRole('button', { name: `Sync ${ENV}` })).toBeDisabled();
      await expect(row.getByRole('button', { name: `Sync ${ENV}` })).toHaveAccessibleDescription(REASON);
      await expect(row.getByRole('button', { name: `Dry run ${ENV}` })).toBeEnabled();

      // nothing anywhere on the rule page is an enabled Sync
      await rule.open(RULE);
      for (const button of await page.getByRole('button', { name: /^Sync\b/ }).all()) await expect(button).toBeDisabled();
    } finally {
      await adminApi.succeeded((await adminApi.sync(RULE, [ENV]))[0]);
    }
  });

  test('Dry run opens the panel as a dry run, and its log says what a sync would add and change, with no way to sync', async ({ page, adminApi, stores }) => {
    const level = await drift(adminApi, stores);
    try {
      const rule = new RulePage(page);
      await rule.open(RULE);
      await rule.envDryRun(ENV).click();
      await expect(rule.sync.dialog).toBeVisible();
      await expect(rule.sync.dialog).toContainText('Dry run');
      await expect(rule.sync.dryRun, 'nothing to choose').toHaveCount(0);
      await expect(rule.sync.dialog).toContainText("You can read this rule's repository but not write to it, so you can dry-run it but not sync it.");
      await expect(rule.sync.start).toHaveText('Start dry run');

      const [taskId] = await rule.sync.startAndCollect();
      // the panel stays open and shows the log
      await expect(rule.sync.dialog).toBeVisible();
      await expect(rule.sync.log).toBeVisible();
      const report = rule.sync.report(ENV);
      await expect(report.getByLabel('Pipeline stages')).toContainText('Read from Git');
      await expect(report.getByLabel('Pipeline stages')).toContainText('Compare with the sink');
      await expect(report.getByLabel('Pipeline stages')).toContainText('Write to sink (skipped: dry run)');
      await expect(report.getByTestId('schema-result')).toContainText('passed its schema');
      await expect(rule.sync.verdicts).toContainText(/A sync of develop would add 1 key, change 1 and delete/);
      await expect(report.getByTestId('would-add')).toContainText('app.name');
      await expect(report.getByTestId('would-change-keys')).toContainText('observability.log_level');
      await expect(report.getByTestId('would-change-keys')).toContainText(level);
      // someone who may not sync is not offered one
      await expect(rule.sync.syncThese).toHaveCount(0);
      await expect(rule.sync.runAgain).toBeEnabled();
      await expect(rule.sync.back).toBeEnabled();

      // nothing was written
      expect(stores.redisKeys(DB, `${PREFIX}app_name`), 'the deleted key is still deleted').toEqual([]);
      expect((await adminApi.finished(taskId)).dryRun).toBe(true);

      // Run again replaces the log; Back returns to the options, the environment still chosen
      const [again] = await (async () => {
        const answered = page.waitForResponse(r => /\/v1\/ui\/rules\/.+\/sync/.test(r.url()) && r.request().method() === 'POST');
        await rule.sync.runAgain.click();
        return ((await (await answered).json()) as { taskIds: string[] }).taskIds;
      })();
      expect(again).not.toBe(taskId);
      await expect(rule.sync.verdicts).toContainText(/would add 1 key, change 1/);
      await rule.sync.back.click();
      await expect(rule.sync.log).toHaveCount(0);
      await expect(rule.sync.environment(ENV)).toBeChecked();
      await expect(rule.sync.start).toHaveText('Start dry run');
      await rule.sync.cancel.click();
      await expect(rule.sync.dialog).toBeHidden();
    } finally {
      await adminApi.succeeded((await adminApi.sync(RULE, [ENV]))[0]);
    }
  });

  test('a file the schema rejects: the log shows the error in full and says a sync would be refused', async ({ page, gitea }) => {
    const good = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', 'develop'))!;
    try {
      await gitea.put(CONFIG_REPO, 'app-settings.yaml', good.replace(/log_level: \w+/, 'log_level: VERBOSE'), 'ui-e2e: invalid log level', 'develop');
      const rule = new RulePage(page);
      await rule.open(RULE);
      await rule.envDryRun(ENV).click();
      await rule.sync.startAndCollect();
      await expect(rule.sync.verdicts).toContainText(/Validation failed: .*observability\.log_level.*VERBOSE.* a sync would be refused/s);
      // the reason in full, as it was said
      await expect(rule.sync.log.getByTestId('dryrun-error')).toContainText('invalid value "VERBOSE"');
      await expect(rule.sync.log.getByTestId('dryrun-error')).toContainText('observability.log_level');
      await expect(rule.sync.log.getByLabel('Pipeline stages')).toContainText('Parse & validate(failed)');
      await expect(rule.sync.log.getByLabel('Pipeline stages')).toContainText('Read from Git(done)');
      await expect(rule.sync.syncThese).toHaveCount(0);
    } finally {
      await gitea.put(CONFIG_REPO, 'app-settings.yaml', good, 'ui-e2e: put the file back', 'develop');
    }
  });

  test('an earlier dry run is in the history with a Show log, which reopens its log over the rule, and the address can be shared', async ({ page, adminApi, stores }) => {
    await drift(adminApi, stores);
    try {
      const rule = new RulePage(page);
      await rule.open(RULE);
      await rule.envDryRun(ENV).click();
      const [taskId] = await rule.sync.startAndCollect();
      await expect(rule.sync.verdicts).toBeVisible();
      await rule.sync.close.click();

      await page.getByRole('tab', { name: 'History' }).click();
      await page.getByRole('button', { name: `Show log of dry run ${taskId.slice(0, 8)}` }).click();
      const log = page.getByRole('dialog', { name: 'Dry run log' });
      await expect(log).toBeVisible();
      await expect(log.getByTestId('dryrun-verdict')).toContainText(/would add 1 key, change 1/);
      await expect(page).toHaveURL(new RegExp(`[?&]run=${taskId}`));

      // the address shows it again
      await page.reload();
      await expect(page.getByRole('dialog', { name: 'Dry run log' }).getByTestId('dryrun-verdict')).toContainText(/would add 1 key, change 1/);
      await page.getByRole('dialog', { name: 'Dry run log' }).getByRole('button', { name: 'Close' }).click();
      await expect(page).not.toHaveURL(/run=/);

      // a task that is not a dry run of this rule opens nothing
      await page.goto(`/rules/demo/${RULE}?run=${await adminApi.sync(RULE, [ENV]).then((ids: string[]) => ids[0])}`);
      await expect(page.getByRole('dialog', { name: 'Dry run log' })).toHaveCount(0);
      await expect(page).not.toHaveURL(/run=/);
    } finally {
      await adminApi.succeeded((await adminApi.sync(RULE, [ENV]))[0]);
    }
  });
});

test.describe('as admin', () => {
  test.use(as('admin'));

  test('a dry run ends in its log, and the real sync of what it checked is one button away; with nothing to change it is off', async ({ page, api, stores, gitea }) => {
    const rule = new RulePage(page);
    const HASH = 'ui-redis-hash-staging:vartrack';
    const settings = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', 'develop'))!;
    const value = unique('checked');
    try {
      // the sink holds what Git says; then Git has a new value
      await api.succeeded((await api.sync('gitea/redis-hash', ['staging']))[0]);
      await gitea.put(CONFIG_REPO, 'app-settings.yaml', settings.replace(/(app:\r?\n\s+name: )[^\r\n]*/, `$1${value}`), 'ui-e2e: a value to check', 'develop');
      const before = stores.redisGet(2, HASH, 'app.name');

      await rule.open('gitea/redis-hash');
      await rule.envSync('staging').click();
      await rule.sync.dryRun.setChecked(true);
      await rule.sync.startAndCollect();
      await expect(rule.sync.verdicts).toContainText(/A sync of staging would add none, change 1 and delete/);
      await expect(rule.sync.report('staging').getByTestId('would-change-keys')).toContainText(value);
      expect(stores.redisGet(2, HASH, 'app.name'), 'the dry run wrote nothing').toBe(before);

      // Sync these environments: the real sync of the same environment, from the panel
      await expect(rule.sync.syncThese).toBeEnabled();
      const answered = page.waitForResponse(startedSync); // the real sync, not the preview a pruning rule's panel makes
      await rule.sync.syncThese.click();
      const real = (await (await answered).json()) as { taskIds: string[] };
      await expect(rule.sync.dialog).toBeHidden();
      const task = await api.succeeded(real.taskIds[0]);
      expect(task.dryRun).toBeFalsy();
      await expect(page.getByRole('status').filter({ hasText: 'Synced staging' })).toBeVisible();
      await expect.poll(() => stores.redisGet(2, HASH, 'app.name')).toBe(value);

      // now a sync would change nothing: the button is off, and says so
      await rule.envSync('staging').click();
      await rule.sync.dryRun.setChecked(true);
      await rule.sync.startAndCollect();
      await expect(rule.sync.verdicts).toContainText('staging already matches Git — a sync would change nothing.');
      await expect(rule.sync.syncThese).toBeDisabled();
      await expect(rule.sync.syncThese).toHaveAttribute('title', 'A sync would change nothing.');

      // Back: the options, with the environment still chosen and Dry run still ticked
      await rule.sync.back.click();
      await expect(rule.sync.environment('staging')).toBeChecked();
      await expect(rule.sync.dryRun).toBeChecked();
      await rule.sync.cancel.click();
    } finally {
      await gitea.put(CONFIG_REPO, 'app-settings.yaml', settings, 'ui-e2e: put the file back', 'develop');
    }
  });

  test('what someone who may sync has is unchanged: Sync from Git, environments to tick, Dry run as an option, and a real sync closes the panel', async ({ page }) => {
    const rule = new RulePage(page);
    await rule.open('gitea/mongo');
    await expect(rule.syncAll).toBeEnabled();
    await rule.syncAll.click();
    await expect(rule.sync.dialog).toContainText('Sync from Git');
    await expect(rule.sync.dryRun).toBeEnabled();
    await expect(rule.sync.dryRun).not.toBeChecked();
    await expect(rule.sync.environment('staging')).toBeVisible();
    await rule.sync.cancel.click();

    await rule.dryRunAll.click();
    await expect(rule.sync.dryRun).toBeChecked();
    await rule.sync.cancel.click();
  });
});
