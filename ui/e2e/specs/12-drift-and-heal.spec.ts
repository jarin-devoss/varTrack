import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, as, unique } from '../fixtures';
import { SCHEMA_REPO } from '../fixtures/gitea';
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
const tamper = (stores: Stores, collection: string, value: string) => stores.mongoSet(collection, 'staging', FIELD, value);
const stored = (stores: Stores, collection: string) => stores.mongoValue(collection, 'staging', FIELD);

const setValidator = (stores: Stores, collection: string, validator: string) =>
  stores.mongo(`db.runCommand({ collMod: ${JSON.stringify(collection)}, validator: ${validator}, validationLevel: ${validator === '{}' ? "'off'" : "'strict'"}, validationAction: 'error' })`);

test.describe('a rule with self-heal (gitea/mongo)', () => {
  const RULE = 'gitea/mongo';
  const COLLECTION = 'ui-mongo';

  // each test starts from the sink as Git says
  test.beforeEach(async ({ adminApi }) => {
    const [id] = await adminApi.sync(RULE, ['staging']);
    await adminApi.succeeded(id);
  });

  test('the watcher is asked to look, finds the change, and puts the value back from Git', async ({ page, api, stack, stores }) => {
    const original = stored(stores, COLLECTION);
    const changed = unique('outside');
    const healed = async () => (await api.events()).filter(e => e.rule === RULE && e.reason === 'HealSucceeded').length;
    const before = await healed();

    const rule = new RulePage(page);
    await rule.open(RULE);
    await expect(rule.env('staging')).toHaveAccessibleName(/staging: Synced/);

    tamper(stores, COLLECTION, changed);
    expect(stored(stores, COLLECTION)).toBe(changed);

    const [found] = await stack.checkNow('gitea', 'mongo');
    expect(found.state, found.detail).toBe('drifted');
    expect(found.healed, 'the watcher asked for a heal').toBe(true);

    await expect.poll(async () => stored(stores, COLLECTION), { timeout: 30_000, message: 'the heal writes the value back' }).toBe(original);
    await expect.poll(healed, { timeout: 30_000 }).toBeGreaterThan(before);
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(rule.env('staging')).toHaveAccessibleName(/staging: Synced/);
    await rule.showRuleTab('Events');
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
    expect((await api.fresh(RULE)).targets.find(t => t.env === 'staging')?.drift).toBe('in_sync');
  });
});

test.describe('a rule without self-heal (gitea/mongo-primary)', () => {
  const RULE = 'gitea/mongo-primary';
  const COLLECTION = 'ui-mongo-primary';

  test.beforeEach(async ({ adminApi }) => {
    const [id] = await adminApi.sync(RULE, ['staging']);
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
    await expect(rule.env('staging')).toHaveAccessibleName(/staging: OutOfSync/);
    await expect(page.locator('main')).toContainText("The watcher doesn't compare this rule with Git. Sync to restore Git values.");
    const view = await api.fresh(RULE);
    expect(view.selfHeal, 'the rule does not ask for self-heal').toBe(false);
    expect(view.targets.find(t => t.env === 'staging')?.drift).toBe('drifted');

    // nobody puts it back: asked to look, the watcher says this rule is not one it keeps
    await expect(stack.checkNow('gitea', 'mongo-primary')).rejects.toThrow(/not watched/);
    expect(stored(stores, COLLECTION)).toBe(changed);

    // Sync repairs it
    const taskId = await rule.syncEnv('staging', { dryRun: false });
    await api.succeeded(taskId);
    expect(stored(stores, COLLECTION)).toBe(original);
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(rule.env('staging')).toHaveAccessibleName(/staging: Synced/);
  });
});

test.describe('a rule with self-heal whose heal the sink refuses (gitea/mongo)', () => {
  const RULE = 'gitea/mongo';
  const COLLECTION = 'ui-mongo';

  test.beforeEach(async ({ adminApi }) => {
    const [id] = await adminApi.sync(RULE, ['staging']);
    await adminApi.succeeded(id);
  });
  test.afterEach(async ({ stores }) => setValidator(stores, COLLECTION, '{}'));

  test('the watcher tries and fails, the rule says self-heal gave up, and Sync mends it once the sink accepts writes', async ({ page, api, stack, stores }) => {
    const original = stored(stores, COLLECTION);
    const changed = unique('outside');
    const failures = async () => (await api.events()).filter(e => e.rule === RULE && e.reason === 'HealFailed').length;
    const before = await failures();

    // the collection refuses every write of the staging environment (as a validator on the database would), and a value is
    // changed past it: the watcher sees the difference, tries to put the value back, and is refused
    setValidator(stores, COLLECTION, "{ $expr: { $ne: ['$_vt_env', 'staging'] } }");
    stores.mongo(`db.runCommand({ update: ${JSON.stringify(COLLECTION)}, updates: [{ q: { _vt_env: 'staging' }, u: { $set: { ${JSON.stringify(`data.${FIELD}`)}: ${JSON.stringify(changed)} } } }], bypassDocumentValidation: true })`);
    expect(stored(stores, COLLECTION)).toBe(changed);

    const [found] = await stack.checkNow('gitea', 'mongo');
    expect(found.healed, 'the watcher asked for a heal').toBe(true);
    await expect.poll(async () => (await api.fresh(RULE)).targets.find(t => t.env === 'staging')?.drift, { timeout: 60_000, intervals: [2_000], message: 'self-heal gives up' }).toBe('heal_failed');
    expect(stored(stores, COLLECTION), 'the sink kept the changed value').toBe(changed);

    const rule = new RulePage(page);
    await rule.open(RULE);
    await expect(rule.env('staging')).toHaveAccessibleName(/staging: OutOfSync/);
    await expect(page.locator('main')).toContainText('self-heal gave up');
    await expect.poll(failures, { timeout: 30_000 }).toBeGreaterThan(before);

    // the cause is gone: the person presses Sync
    setValidator(stores, COLLECTION, '{}');
    await api.succeeded(await rule.syncEnv('staging', { dryRun: false }));
    expect(stored(stores, COLLECTION)).toBe(original);
    await expect.poll(async () => (await api.fresh(RULE)).targets.find(t => t.env === 'staging')?.drift, { timeout: 30_000 }).toBe('in_sync');
  });
});

test.describe('a document rule set to GIT_SMART_REPAIR (gitea/mongo)', () => {
  const RULE = 'gitea/mongo';
  const COLLECTION = 'ui-mongo';
  const world = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'world', 'bundle.json'), 'utf8');

  // The rule compares what the document holds with Git, not the commit that stamped it: the heal runs at the commit
  // that is already there, so a document someone edited was "unchanged" and was never put back.
  test.beforeEach(async ({ gitea, stack, adminApi }) => {
    const rules = JSON.parse(world) as Array<Record<string, unknown>>;
    Object.assign(rules.find(r => r.datasource === 'mongo')!, { sync_mode: 'GIT_SMART_REPAIR' });
    await gitea.put(SCHEMA_REPO, 'bundle.json', JSON.stringify(rules, null, 2), 'ui-e2e: gitea/mongo repairs smartly', 'main');
    await stack.warm();
    await adminApi.succeeded((await adminApi.sync(RULE, ['staging']))[0]);
  });
  test.afterEach(async ({ gitea, stack }) => {
    await gitea.put(SCHEMA_REPO, 'bundle.json', world, 'ui-e2e: the rules, as they were', 'main');
    await stack.warm();
  });

  test('a document edited in MongoDB is put back by the heal, at the commit it was written from', async ({ stack, stores }) => {
    const original = stored(stores, COLLECTION);
    tamper(stores, COLLECTION, unique('outside'));

    const [found] = await stack.checkNow('gitea', 'mongo');
    expect(found.state, found.detail).toBe('drifted');
    expect(found.healed, 'the watcher asked for a heal').toBe(true);
    await expect.poll(async () => stored(stores, COLLECTION), { timeout: 30_000, message: 'the heal writes the document back' }).toBe(original);
  });

  test('a sync of a document that already holds the file says there was nothing to change, and writes nothing', async ({ adminApi }) => {
    const task = await adminApi.succeeded((await adminApi.sync(RULE, ['staging']))[0]);
    expect(task.message).toMatch(/^nothing to change \(\d+ keys? already match(es)? Git\)/);
    expect(task.files?.every(f => f.written === 0)).toBe(true);
  });
});
