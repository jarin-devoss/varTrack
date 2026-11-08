import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, as, unique } from '../fixtures';
import { CONFIG_REPO, SCHEMA_REPO } from '../fixtures/gitea';
import { RulePage } from '../pages/RulePage';

/**
 * An environment a rule produces and has not written yet is on the rule's page, ready for its first sync ("Not synced yet"),
 * instead of the page showing only what was written before. The rule here stores one document per key (STRATEGY_KEY_VALUE),
 * so the spec also shows that shape, and a prune of a stale key document.
 * covers: state:drift.missing, action:envbox.forget, state:mongo.keyvalue
 */
test.use(as('admin'));

const RULE = 'gitea/mongo-kv';
const COLLECTION = 'ui-mongo-kv';
const here = path.dirname(fileURLToPath(import.meta.url));
const world = fs.readFileSync(path.join(here, '..', 'world', 'bundle.json'), 'utf8');
const EXTRA = 'extra:\n  stale: value\n  other: value\n';

test('the first sync of an environment nobody has written writes it, one document per key, and a later sync deletes a stale key document', async ({
  page, adminApi, gitea, stack, stores,
}) => {
  const env = unique('first').toLowerCase();
  const rules = JSON.parse(world) as Array<Record<string, unknown>>;
  rules.push({
    platform: 'gitea',
    datasource: 'mongo-kv',
    file_name: 'app-settings.yaml',
    branch_map: { main: env },
    destination_template: COLLECTION,
    prune: { enabled: true },
  });
  const main = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', 'main'))!;
  const rule = new RulePage(page);

  try {
    await gitea.put(SCHEMA_REPO, 'bundle.json', JSON.stringify(rules, null, 2), 'ui-e2e: a rule that has written nothing yet');
    await stack.warm();

    // 1. listed before it is written, ready to sync
    await rule.open(RULE);
    const box = rule.env(env);
    await expect(box).toBeVisible();
    await expect(box).toContainText('Not synced yet');
    await expect(box).toContainText('nothing written here yet');
    await expect(rule.envSync(env)).toBeEnabled();
    expect(stores.mongoCount(COLLECTION, `{ _vt_env: ${JSON.stringify(env)} }`)).toBe(0);

    // 2. the first sync writes it: one document per key
    await adminApi.succeeded(await rule.syncEnv(env, { dryRun: false }));
    await expect(rule.env(env)).toHaveAccessibleName(new RegExp(`${env}: Synced`));
    const keys = stores.mongoCount(COLLECTION, `{ _vt_env: ${JSON.stringify(env)}, key: { $exists: true } }`);
    expect(keys, 'one document per key').toBeGreaterThan(10);
    expect(stores.mongoKeyValue(COLLECTION, env, 'api.rate_limit')).toBe('1000');

    // 3. keys Git no longer has are deleted by the next sync, and the panel says which first
    await gitea.put(CONFIG_REPO, 'app-settings.yaml', `${main.trimEnd()}\n${EXTRA}`, 'ui-e2e: extra keys', 'main');
    await adminApi.succeeded(await rule.syncEnv(env, { dryRun: false }));
    expect(stores.mongoCount(COLLECTION, `{ _vt_env: ${JSON.stringify(env)}, key: { $exists: true } }`)).toBe(keys + 2);

    await gitea.put(CONFIG_REPO, 'app-settings.yaml', main, 'ui-e2e: take the extra keys out', 'main');
    await rule.open(RULE);
    await rule.envSync(env).click();
    await rule.sync.dryRun.setChecked(false);
    await expect(page.getByTestId('prune-preview')).toContainText('This sync deletes 2 keys');
    await expect(page.getByTestId('prune-preview')).toContainText('extra.stale');
    await expect(rule.sync.start).toHaveText('Sync and delete 2 keys');
    await rule.sync.cancel.click();
    await adminApi.succeeded(await rule.syncEnv(env, { dryRun: false }));
    expect(stores.mongoCount(COLLECTION, `{ _vt_env: ${JSON.stringify(env)}, key: { $exists: true } }`), 'the stale key documents are gone').toBe(keys);
  } finally {
    await gitea.put(CONFIG_REPO, 'app-settings.yaml', main, 'ui-e2e: the file as it was', 'main');
    await gitea.put(SCHEMA_REPO, 'bundle.json', world, 'ui-e2e: the rules, as they were');
    await stack.warm();
  }

  // 4. the rule no longer produces that environment: it is left over, and an administrator forgets it
  await rule.open(RULE);
  await expect(rule.env(env)).toContainText('No longer synced by this rule');
  await rule.env(env).getByRole('button', { name: `Forget ${env}` }).click();
  await expect(rule.env(env)).toHaveCount(0);
  stores.mongo(`db.getCollection(${JSON.stringify(COLLECTION)}).deleteMany({ _vt_env: ${JSON.stringify(env)} })`);
});
