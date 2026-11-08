import { expect, test, as, unique, type Role } from '../fixtures';
import { CONFIG_REPO } from '../fixtures/gitea';
import { RulePage } from '../pages/RulePage';

/**
 * A dry run runs the whole pipeline and writes nothing. covers: action:rule.dryrun, action:syncpanel.dryrun,
 * action:toast.dismiss, state:task.dryrun, state:file.checked, state:validation.ok, state:validation.failed
 */

const HASH = 'ui-redis-hash-staging:vartrack';
const DB = 2;

for (const role of ['admin', 'writer', 'reader'] as Role[]) {
  test.describe(`as ${role}`, () => {
    test.use(as(role));

    test('the Dry run button starts a dry run of every environment: the task is marked, its files are checked, the sink is unchanged', async ({ page, api, stores, gitea }) => {
      // Git has a new value; a dry run must report it without writing it.
      const value = unique(`dry-${role}`);
      const settings = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', 'develop'))!;
      const before = stores.redisGet(DB, HASH, 'app.name');
      try {
        await gitea.put(CONFIG_REPO, 'app-settings.yaml', settings.replace(/(app:\r?\n\s+name: )[^\r\n]*/, `$1${value}`), 'ui-e2e: dry run', 'develop');

        const rule = new RulePage(page);
        await rule.open('gitea/redis-hash');
        await rule.dryRunAll.click();
        await expect(rule.sync.dialog).toBeVisible();
        await expect(rule.sync.dryRun).toBeChecked();
        const taskIds = await rule.sync.startAndCollect();
        expect(taskIds, 'one task per environment the rule produces').toHaveLength(2);

        // the toast says how it ended: both environments passed, nothing written; it links to the task, and can be dismissed
        const toast = page.getByRole('status').filter({ hasText: 'Dry run of 2 environments passed' });
        await expect(toast).toBeVisible();
        await expect(toast).toContainText(/\d+ keys checked, nothing written/);
        await expect(toast.getByRole('link', { name: 'Details' })).toHaveAttribute('href', `/tasks/${taskIds[0]}`);
        await toast.getByRole('button', { name: 'Dismiss' }).click();
        await expect(toast).toBeHidden();

        for (const taskId of taskIds) {
          const task = await api.finished(taskId);
          expect(task.state, task.message).toBe('SUCCEEDED');
          expect(task.dryRun).toBe(true);
          for (const file of task.files ?? []) {
            expect(file.validation, file.file).toBe('ok');
            expect(file.schema).toBe('app-settings.yaml.cue');
            expect(file.schemaSource).toBe('name');
          }
        }
        expect(stores.redisGet(DB, HASH, 'app.name'), 'a dry run writes nothing').toBe(before);
      } finally {
        await gitea.put(CONFIG_REPO, 'app-settings.yaml', settings, 'ui-e2e: put the file back', 'develop');
      }
    });

    test('the task page marks it as a dry run and shows its files as checked, not written', async ({ page, api }) => {
      const rule = new RulePage(page);
      await rule.open('gitea/redis-hash');
      await rule.dryRunAll.click();
      const [taskId] = await rule.sync.startAndCollect();
      await api.finished(taskId);

      await page.goto(`/tasks/${taskId}`);
      await expect(page.getByText('Dry run').first()).toBeVisible();
      await expect(page.locator('.pill').filter({ hasText: /^Checked$/ }).first()).toBeVisible();
      await expect(page.locator('.pill').filter({ hasText: /^Written$/ })).toHaveCount(0);   // the column heading is not a status
    });
  });
}

test.describe('a rule with strict_validation', () => {
  test.use(as('admin'));

  test('a file the schema rejects fails the dry run, names the key, and nothing is written', async ({ page, api, gitea, stores }) => {
    // gitea/redis-string is strict. Its develop environment reads app-settings.yaml on develop.
    const good = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', 'develop'))!;
    const before = stores.redisGet(3, 'ui-redis-string-develop:observability_log_level');
    try {
      await gitea.put(CONFIG_REPO, 'app-settings.yaml', good.replace(/log_level: \w+/, 'log_level: VERBOSE'), 'ui-e2e: invalid log level', 'develop');

      const rule = new RulePage(page);
      await rule.open('gitea/redis-string');
      const taskId = await rule.syncEnv('develop', { dryRun: true });
      const task = await api.finished(taskId);

      expect(task.state).toBe('FAILED');
      expect(task.message).toMatch(/strict_validation/);
      expect(task.message).toMatch(/observability\.log_level/);
      expect(task.message).toMatch(/VERBOSE/);
      expect(stores.redisGet(3, 'ui-redis-string-develop:observability_log_level')).toBe(before);

      await page.goto(`/tasks/${taskId}`);
      await expect(page.getByText('Failed').first()).toBeVisible();
      await expect(page.getByText(/VERBOSE/).first()).toBeVisible();
    } finally {
      await gitea.put(CONFIG_REPO, 'app-settings.yaml', good, 'ui-e2e: put the file back', 'develop');
    }
  });
});
