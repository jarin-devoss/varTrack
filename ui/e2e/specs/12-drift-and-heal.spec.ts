import { expect, test, as, unique } from '../fixtures';
import type { Stores } from '../fixtures/stores';
import { RulePage } from '../pages/RulePage';

/**
 * A value changed directly in a sink. A rule with self-heal is put back by the watcher; a rule without it stays out of
 * sync until someone presses Sync. The spec does not wait for the watcher's poll: it asks the watcher to look now
 * (`stack.checkNow`), the way the product's own Refresh asks for a fresh read.
 * covers: state:drift.drifted, state:drift.healing, state:drift.in_sync, state:drift.not_watched,
 * state:sync.OutOfSync, state:sync.Synced, state:event.heal
 */
test.use(as('admin'));

const FIELD = 'api.rate_limit';

// The rules store a file as one document per environment (STRATEGY_DOCUMENT): the value is nested under `data`.
const tamper = (stores: Stores, collection: string, value: string) => stores.mongoSet(collection, 'default', FIELD, value);
const stored = (stores: Stores, collection: string) => stores.mongoValue(collection, 'default', FIELD);

const setValidator = (stores: Stores, collection: string, validator: string) =>
  stores.mongo(`db.runCommand({ collMod: ${JSON.stringify(collection)}, validator: ${validator}, validationLevel: ${validator === '{}' ? "'off'" : "'strict'"}, validationAction: 'error' })`);

test.describe('a rule with self-heal (gitea/mongo)', () => {
  const RULE = 'gitea/mongo';
  const COLLECTION = 'ui-mongo';

  // each test starts from the sink as Git says
  test.beforeEach(async ({ adminApi }) => {
    const [id] = await adminApi.sync(RULE, ['default']);
    await adminApi.succeeded(id);
  });

  test('the watcher is asked to look, finds the change, and puts the value back from Git', async ({ page, api, stack, stores }) => {
    const original = stored(stores, COLLECTION);
    const changed = unique('outside');
    const healed = async () => (await api.events()).filter(e => e.rule === RULE && e.reason === 'HealSucceeded').length;
    const before = await healed();

    const rule = new RulePage(page);
    await rule.open(RULE);
    await expect(rule.env('default')).toHaveAccessibleName(/default: Synced/);

    tamper(stores, COLLECTION, changed);
    expect(stored(stores, COLLECTION)).toBe(changed);

    const [found] = await stack.checkNow('gitea', 'mongo');
    expect(found.state, found.detail).toBe('drifted');
    expect(found.healed, 'the watcher asked for a heal').toBe(true);

    await expect.poll(async () => stored(stores, COLLECTION), { timeout: 30_000, message: 'the heal writes the value back' }).toBe(original);
    await expect.poll(healed, { timeout: 30_000 }).toBeGreaterThan(before);
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(rule.env('default')).toHaveAccessibleName(/default: Synced/);
    await page.getByRole('tab', { name: 'Events' }).click();
    await expect(page.locator('#rule-panel')).toContainText(/heal/i);
  });

  test('the rule page says what self-heal is doing', async ({ page }) => {
    const rule = new RulePage(page);
    await rule.open(RULE);
    await expect(page.locator('main')).toContainText(/self-heal|Restoring|healing|in sync/i);
    await expect(page.getByText("The watcher doesn't compare this rule with Git")).toHaveCount(0);
  });

  test('the watcher is not told twice: a change made after its heal is a new one and is healed at once', async ({ api, stack, stores }) => {
    const original = stored(stores, COLLECTION);
    tamper(stores, COLLECTION, unique('first'));
    expect((await stack.checkNow('gitea', 'mongo'))[0].healed).toBe(true);
    await expect.poll(async () => stored(stores, COLLECTION), { timeout: 30_000 }).toBe(original);

    tamper(stores, COLLECTION, unique('second'));
    const [again] = await stack.checkNow('gitea', 'mongo');
    expect(again.state).toBe('drifted');
    expect(again.healed).toBe(true);
    await expect.poll(async () => stored(stores, COLLECTION), { timeout: 30_000 }).toBe(original);
    expect((await api.fresh(RULE)).targets.find(t => t.env === 'default')?.drift).toBe('in_sync');
  });
});

test.describe('a rule without self-heal (gitea/mongo-primary)', () => {
  const RULE = 'gitea/mongo-primary';
  const COLLECTION = 'ui-mongo-primary';

  test.beforeEach(async ({ adminApi }) => {
    const [id] = await adminApi.sync(RULE, ['default']);
    await adminApi.succeeded(id);
  });

  test('it stays out of sync, the watcher does not look at it, and Sync puts the value back', async ({ page, api, stack, stores }) => {
    const original = stored(stores, COLLECTION);
    const changed = unique('outside');
    const rule = new RulePage(page);
    await rule.open(RULE);

    tamper(stores, COLLECTION, changed);
    // Refresh reads the sink now
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(rule.env('default')).toHaveAccessibleName(/default: OutOfSync/);
    await expect(page.locator('main')).toContainText("The watcher doesn't compare this rule with Git. Sync to restore Git values.");
    const view = await api.fresh(RULE);
    expect(view.selfHeal, 'the rule does not ask for self-heal').toBe(false);
    expect(view.targets.find(t => t.env === 'default')?.drift).toBe('drifted');

    // nobody puts it back: asked to look, the watcher says this rule is not one it keeps
    await expect(stack.checkNow('gitea', 'mongo-primary')).rejects.toThrow(/not watched/);
    expect(stored(stores, COLLECTION)).toBe(changed);

    // Sync repairs it
    const taskId = await rule.syncEnv('default', { dryRun: false });
    await api.succeeded(taskId);
    expect(stored(stores, COLLECTION)).toBe(original);
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(rule.env('default')).toHaveAccessibleName(/default: Synced/);
  });
});

test.describe('a rule with self-heal whose heal the sink refuses (gitea/mongo)', () => {
  const RULE = 'gitea/mongo';
  const COLLECTION = 'ui-mongo';

  test.beforeEach(async ({ adminApi }) => {
    const [id] = await adminApi.sync(RULE, ['default']);
    await adminApi.succeeded(id);
  });
  test.afterEach(async ({ stores }) => setValidator(stores, COLLECTION, '{}'));

  test('the watcher tries and fails, the rule says self-heal gave up, and Sync mends it once the sink accepts writes', async ({ page, api, stack, stores }) => {
    const original = stored(stores, COLLECTION);
    const changed = unique('outside');
    const failures = async () => (await api.events()).filter(e => e.rule === RULE && e.reason === 'HealFailed').length;
    const before = await failures();

    // the collection refuses every write of the default environment (as a validator on the database would), and a value is
    // changed past it: the watcher sees the difference, tries to put the value back, and is refused
    setValidator(stores, COLLECTION, "{ $expr: { $ne: ['$_vt_env', 'default'] } }");
    stores.mongo(`db.runCommand({ update: ${JSON.stringify(COLLECTION)}, updates: [{ q: { _vt_env: 'default' }, u: { $set: { ${JSON.stringify(`data.${FIELD}`)}: ${JSON.stringify(changed)} } } }], bypassDocumentValidation: true })`);
    expect(stored(stores, COLLECTION)).toBe(changed);

    const [found] = await stack.checkNow('gitea', 'mongo');
    expect(found.healed, 'the watcher asked for a heal').toBe(true);
    await expect.poll(async () => (await api.fresh(RULE)).targets.find(t => t.env === 'default')?.drift, { timeout: 60_000, intervals: [2_000], message: 'self-heal gives up' }).toBe('heal_failed');
    expect(stored(stores, COLLECTION), 'the sink kept the changed value').toBe(changed);

    const rule = new RulePage(page);
    await rule.open(RULE);
    await expect(rule.env('default')).toHaveAccessibleName(/default: OutOfSync/);
    await expect(page.locator('main')).toContainText('self-heal gave up');
    await expect.poll(failures, { timeout: 30_000 }).toBeGreaterThan(before);

    // the cause is gone: the person presses Sync
    setValidator(stores, COLLECTION, '{}');
    await api.succeeded(await rule.syncEnv('default', { dryRun: false }));
    expect(stored(stores, COLLECTION)).toBe(original);
    await expect.poll(async () => (await api.fresh(RULE)).targets.find(t => t.env === 'default')?.drift, { timeout: 30_000 }).toBe('in_sync');
  });
});
