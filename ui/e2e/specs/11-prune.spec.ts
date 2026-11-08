import { expect, test, as, unique } from '../fixtures';
import { CONFIG_REPO } from '../fixtures/gitea';
import { RulePage, startedSync } from '../pages/RulePage';

/**
 * Pruning removes the keys varTrack wrote that Git no longer has. Whether a sync prunes is the rule's setting: the panel
 * shows it, it asks for confirmation before deleting from a production-like environment, and the rule's protected keys
 * survive. covers: action:syncpanel.prune, action:prune.confirm, action:prune.cancel, state:prune.disabled, state:prune.protected,
 * state:prune.preview
 *
 * Before a sync that prunes, a dry run says which keys it would delete (it is not listed among the tasks): the panel shows them,
 * a sync that deletes nothing asks nothing, one that deletes from a non-production environment says so on its button, and only
 * one that deletes from a production-like environment asks for the typed word, with the keys listed.
 */
test.use(as('admin'));

// whatever a test leaves in the sink, the next one does not start from
test.afterEach(async ({ stores }) => {
  for (const { hash } of Object.values(ENVS)) stores.redis(DB, 'HDEL', hash, 'extra.stale', 'extra.keep.me');
});

const RULE = 'gitea/redis-hash'; // prune on, protecting extra.keep.*
const DB = 2;
const EXTRA = 'extra:\n  stale: value\n  keep:\n    me: value\n';

/** Where the rule keeps an environment, and the branch that feeds it. */
const ENVS = {
  staging: { hash: 'ui-redis-hash-staging:vartrack', branch: 'develop' },
  production: { hash: 'ui-redis-hash-production:vartrack', branch: 'main' },
} as const;

type Ctx = { page: import('@playwright/test').Page; api: import('../fixtures/api').UiApi; gitea: import('../fixtures/gitea').Gitea; stores: import('../fixtures/stores').Stores };

/**
 * Put the extra keys in Git, sync them into the sink, then take them out of Git again: they are now stale.
 * (The rule prunes, so the sync that writes them has nothing to prune yet.)
 */
async function makeStale(env: keyof typeof ENVS, { page, api, gitea, stores }: Ctx): Promise<void> {
  const { hash, branch } = ENVS[env];
  const clean = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', branch))!;
  await gitea.put(CONFIG_REPO, 'app-settings.yaml', `${clean.trimEnd()}\n${EXTRA}`, `ui-e2e: extra keys (${unique('p')})`, branch);

  const rule = new RulePage(page);
  await rule.open(RULE);
  const written = await api.finished(await rule.syncEnv(env, { dryRun: false }));
  expect(written.state, written.message).toBe('SUCCEEDED');
  expect(stores.redisGet(DB, hash, 'extra.stale')).toBe('value');
  expect(stores.redisGet(DB, hash, 'extra.keep.me')).toBe('value');

  await gitea.put(CONFIG_REPO, 'app-settings.yaml', clean, `ui-e2e: take the extra keys out (${unique('p')})`, branch);
}

test('a rule that does not prune says so, and the option is off', async ({ page }) => {
  const rule = new RulePage(page);
  await rule.open('gitea/redis-string');
  await rule.envSync('develop').click();
  await expect(rule.sync.prune).toBeDisabled();
  await expect(rule.sync.prune).not.toBeChecked();
  await expect(rule.sync.dialog).toContainText('Pruning is turned off for this rule in the bundle.');
  await expect(page.getByTestId('prune-preview')).toHaveCount(0);     // nothing to preview: the rule does not prune
});

test('a sync that would delete nothing says so and asks nothing, even for production', async ({ page }) => {
  const rule = new RulePage(page);
  await rule.open(RULE);
  await rule.envSync('production').click();
  await rule.sync.dryRun.setChecked(false);
  await expect(page.getByTestId('prune-preview')).toHaveText('This sync deletes nothing.');
  await expect(rule.sync.dialog).not.toContainText('Prune writes to production');
  await expect(rule.sync.start).toHaveText('Sync 1 environment');
});

test('a rule that prunes says it prunes on every sync, names what it protects, and a dry run does not prune', async ({ page }) => {
  const rule = new RulePage(page);
  await rule.open(RULE);
  await rule.envSync('staging').click();
  await rule.sync.dryRun.setChecked(false);
  await expect(rule.sync.prune).toBeChecked();
  await expect(rule.sync.prune).toBeDisabled();      // shown, not chosen: it is the rule's setting
  await expect(rule.sync.dialog).toContainText('This rule prunes on every sync.');
  await expect(rule.sync.dialog.getByText('extra.keep.*')).toBeVisible();

  await rule.sync.dryRun.setChecked(true);
  await expect(rule.sync.prune).not.toBeChecked();
});

test('a sync of staging says which key it deletes, offers "Sync and delete 1 key", keeps the protected one, and asks no typed word', async ({ page, api, gitea, stores }) => {
  await makeStale('staging', { page, api, gitea, stores });
  const { hash } = ENVS.staging;

  const rule = new RulePage(page);
  await rule.open(RULE);
  await rule.envSync('staging').click();
  await rule.sync.dryRun.setChecked(false);
  const preview = page.getByTestId('prune-preview');
  await expect(preview).toContainText('This sync deletes 1 key');
  await expect(preview).toContainText('extra.stale');
  await expect(preview, 'a protected key is not on the list').not.toContainText('extra.keep.me');
  await expect(rule.sync.start).toHaveText('Sync and delete 1 key');
  const [taskId] = await rule.sync.startAndCollect();
  await expect(page.getByRole('dialog', { name: 'Prune production keys?' })).toHaveCount(0);

  const task = await api.finished(taskId);
  expect(task.state, task.message).toBe('SUCCEEDED');
  expect(stores.redisGet(DB, hash, 'extra.stale'), 'the stale key is deleted').toBe('');
  expect(stores.redisGet(DB, hash, 'extra.keep.me'), 'the protected key survives').toBe('value');
  const pruned = (task.files ?? []).reduce((n, f) => n + ((f as unknown as { pruned?: number }).pruned ?? 0), 0);
  expect(pruned, 'the task reports what the preview said it would').toBe(1);
  await expect(page.getByRole('status').filter({ hasText: 'Synced staging' })).toContainText('1 key deleted');
  stores.redis(DB, 'HDEL', hash, 'extra.keep.me'); // the protected key is the spec's own: take it out
});

test('a dry run of production deletes nothing and asks nothing', async ({ page, api, gitea, stores }) => {
  await makeStale('production', { page, api, gitea, stores });
  const { hash } = ENVS.production;

  const rule = new RulePage(page);
  await rule.open(RULE);
  await rule.envSync('production').click();
  await rule.sync.dryRun.setChecked(true);
  await expect(rule.sync.dialog).not.toContainText('Prune writes to production');
  const [taskId] = await rule.sync.startAndCollect();
  await expect(page.getByRole('dialog', { name: 'Prune production keys?' })).toHaveCount(0);
  expect((await api.finished(taskId)).state).toBe('SUCCEEDED');
  expect(stores.redisGet(DB, hash, 'extra.stale'), 'a dry run deletes nothing').toBe('value');
});

test('a sync of production asks for the word "prune" first, and deletes nothing until it is typed', async ({ page, api, gitea, stores }) => {
  await makeStale('production', { page, api, gitea, stores });
  const { hash } = ENVS.production;
  expect(stores.redisGet(DB, hash, 'extra.stale')).toBe('value');
  const tasksBefore = (await api.tasks()).length;

  const rule = new RulePage(page);
  await rule.open(RULE);
  await rule.envSync('production').click();
  await rule.sync.dryRun.setChecked(false);
  await expect(page.getByTestId('prune-preview')).toContainText('This sync deletes 1 key');
  await expect(rule.sync.dialog).toContainText('Prune writes to production');
  await rule.sync.start.click();

  const confirm = page.getByRole('dialog', { name: 'Prune production keys?' });
  await expect(confirm).toBeVisible();
  await expect(confirm).toContainText('Keys to be deleted from production (1):');
  await expect(confirm.getByRole('listitem')).toHaveText(['extra.stale']);
  const go = confirm.getByRole('button', { name: 'Sync and prune' });
  await expect(go).toBeDisabled();
  await confirm.getByRole('textbox').fill('prun');
  await expect(go).toBeDisabled();

  // Cancel: nothing is deleted and nothing is queued
  await confirm.getByRole('button', { name: 'Cancel' }).click();
  await expect(confirm).toBeHidden();
  expect(stores.redisGet(DB, hash, 'extra.stale')).toBe('value');
  expect((await api.tasks()).length).toBe(tasksBefore);

  // Confirm
  await rule.sync.start.click();
  await confirm.getByRole('textbox').fill('prune');
  await expect(go).toBeEnabled();
  const answered = page.waitForResponse(startedSync);
  await go.click();
  const { taskIds } = (await (await answered).json()) as { taskIds: string[] };
  const task = await api.finished(taskIds[0]);
  expect(task.state, task.message).toBe('SUCCEEDED');
  expect(stores.redisGet(DB, hash, 'extra.stale')).toBe('');
  expect(stores.redisGet(DB, hash, 'extra.keep.me'), 'the protected key survives here too').toBe('value');
  stores.redis(DB, 'HDEL', hash, 'extra.keep.me');
});
