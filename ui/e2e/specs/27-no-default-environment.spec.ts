import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, as, unique } from '../fixtures';
import { CONFIG_REPO, Gitea, SCHEMA_REPO } from '../fixtures/gitea';
import { RulePage } from '../pages/RulePage';

/**
 * There is no "default" environment. A rule has to say where its environments come from: one that does not is refused when it
 * is loaded (listed with why, nothing runs it, an event says so); a push nothing names feeds no environment of the rule and is
 * skipped, never written somewhere nobody declared; the CLI is refused an environment nothing feeds, in the engine's words; and
 * `default` is only a name a branch_map may give, which becomes a leftover like any other when it stops doing so.
 * covers: state:event.RuleRefused, state:rule.refused, state:push.unfed, state:cli.unfed
 */
test.use(as('admin'));

const here = path.dirname(fileURLToPath(import.meta.url));
const world = fs.readFileSync(path.join(here, '..', 'world', 'bundle.json'), 'utf8');
const KV = 'gitea/mongo-kv';
const DR = 'gitea/mongo-dr';
const COLLECTION = 'ui-no-default';

const rulesWith = (change: (rules: Array<Record<string, unknown>>) => void): string => {
  const rules = JSON.parse(world) as Array<Record<string, unknown>>;
  change(rules);
  return JSON.stringify(rules, null, 2);
};

test('no rule produces an environment called default: it is a name a branch_map may give, a leftover when it stops', async ({ page, adminApi, gitea, stack, stores }) => {
  // 1. nothing on the stack is fed by a "default" fallback (a branch_as_env rule takes any branch name, and is not one)
  const { rules } = (await (await page.request.get('/api/orchestrator/v1/ui/rules')).json()) as {
    rules: Array<{ name: string; branchAsEnv: boolean; refused?: string; targets: Array<{ env: string; notFed?: string }> }>;
  };
  for (const r of rules.filter(r => !r.branchAsEnv && !r.refused)) {
    expect(r.targets.filter(t => t.env === 'default' && !t.notFed).map(() => r.name), `${r.name} produces default`).toEqual([]);
  }

  // 2. a branch_map can still name an environment default: main feeds it...
  const rule = new RulePage(page);
  try {
    await gitea.put(SCHEMA_REPO, 'bundle.json', rulesWith(rs => Object.assign(rs.find(r => r.datasource === 'mongo-kv')!, {
      file_name: 'app-settings.yaml', branch_map: { main: 'default' }, destination_template: COLLECTION,
    })), 'ui-e2e: main feeds an environment called default', 'main');
    await stack.warm();
    await adminApi.succeeded((await adminApi.sync(KV, ['default']))[0]);
    await rule.open(KV);
    await expect(rule.env('default')).not.toContainText('No longer synced');
    await expect(rule.envSync('default')).toBeEnabled();

    // 3. ...and when it stops, `default` is a leftover like any other: nothing feeds it, Sync and Diff are off, Forget drops it
    await gitea.put(SCHEMA_REPO, 'bundle.json', world, 'ui-e2e: the rules, as they were', 'main');
    await stack.warm();
    await rule.open(KV);
    await expect(rule.env('default')).toContainText('No longer synced by this rule');
    await expect(rule.envSync('default')).toBeDisabled();
    await expect(rule.envSync('default')).toHaveAttribute('title', /nothing feeds default in the mongo-kv rule/);
    await expect(rule.envDiff('default')).toBeDisabled();
    await rule.env('default').getByRole('button', { name: 'Forget default' }).click();
    await expect(rule.env('default')).toHaveCount(0);
  } finally {
    await gitea.put(SCHEMA_REPO, 'bundle.json', world, 'ui-e2e: the rules, as they were', 'main');
    await stack.warm();
    stores.mongo(`db.getCollection(${JSON.stringify(COLLECTION)}).deleteMany({ _vt_env: 'default' })`);
  }
});

test('a rule that names no environments is refused: listed with why and no actions, an event says so, nothing runs it, and fixing it loads it again', async ({
  page, api, gitea, stack,
}) => {
  const rule = new RulePage(page);
  try {
    await gitea.put(SCHEMA_REPO, 'bundle.json', rulesWith(rs => { delete rs.find(r => r.datasource === 'mongo-dr')!.branch_map; }), 'ui-e2e: a rule with no environments', 'main');
    await stack.warm();

    // listed as refused, with why and how to fix it, and no Sync
    await page.goto('/rules?q=mongo-dr');
    const card = page.getByRole('article', { name: `${DR}: Refused` });
    await expect(card).toBeVisible();
    await expect(card.getByTestId('refused-reason')).toContainText('gitea/mongo-dr names no environments: set branch_map');
    await expect(card.getByTestId('refused-reason')).toContainText('branch_as_env, pr_as_env or tag_as_env');
    await expect(card.getByTestId('refused-reason')).toContainText('branch_map: {"main": "default"}');
    await expect(card.getByRole('button')).not.toContainText(/Sync|Dry run/);

    // its own page says so, with no actions and no environments
    await card.getByRole('link', { name: /Details/ }).click();
    await expect(page.getByText('This rule is refused and not loaded: no push, sync or dry run reaches it.')).toBeVisible();
    await expect(page.getByTestId('refused-reason')).toContainText('gitea/mongo-dr names no environments');
    await expect(page.locator('.action-bar')).toHaveCount(0);
    await expect(page.locator('article.envbox')).toHaveCount(0);

    // a sync or dry run of it is refused with the reason
    for (const dryRun of [false, true]) {
      const response = await page.request.post(`/api/orchestrator/v1/ui/rules/${DR}/sync`, { data: { envs: ['production'], dryRun, prune: false } });
      expect(response.status(), `dryRun=${dryRun}`).toBe(409);
      expect(((await response.json()) as { detail: string }).detail).toMatch(/^gitea\/mongo-dr names no environments/);
    }

    // a push to its repository reaches nothing, and the task says why
    // (a task for a rule nothing loads is not listed among the UI's tasks: its result is the worker's)
    const taskId = await stack.push('mongo-dr', 'main', await gitea.head(CONFIG_REPO, 'main'));
    let result: { state?: string; message?: string } | undefined;
    await expect
      .poll(async () => {
        const body = (await (await page.request.get(`/api/orchestrator/v1/tasks/${taskId}`)).json()) as { state: string; result?: typeof result };
        result = body.result;
        return body.state;
      }, { timeout: 45_000, message: `the worker answers for ${taskId}` })
      .toBe('SUCCESS');
    expect(result?.state).toBe('FAILED');
    expect(result?.message).toMatch(/^gitea\/mongo-dr names no environments/);

    // an event on the tenant says it was refused
    await expect.poll(async () => (await api.events(`&rule=${encodeURIComponent(DR)}`)).filter(e => e.reason === 'RuleRefused').length).toBeGreaterThan(0);
  } finally {
    await gitea.put(SCHEMA_REPO, 'bundle.json', world, 'ui-e2e: the rules, as they were', 'main');
    await stack.warm();
  }

  // named again, it loads again
  await page.goto('/rules?q=mongo-dr');
  await expect(page.getByRole('article', { name: new RegExp(`^${DR}:`) })).not.toContainText('Refused');
  await rule.open(DR);
  await expect(rule.syncAll).toBeEnabled();
});

test('a rule that names no file is refused too: listed with why, no actions, an event says so, and naming one loads it again', async ({ page, api, gitea, stack }) => {
  const rule = new RulePage(page);
  try {
    // keeps its branch_map, so only the file is missing
    await gitea.put(SCHEMA_REPO, 'bundle.json', rulesWith(rs => { delete rs.find(r => r.datasource === 'mongo-dr')!.file_name; }), 'ui-e2e: a rule with no file', 'main');
    await stack.warm();

    await page.goto('/rules?q=mongo-dr');
    const card = page.getByRole('article', { name: `${DR}: Refused` });
    await expect(card).toBeVisible();
    await expect(card.getByTestId('refused-reason')).toContainText('gitea/mongo-dr names no file to read: set file_name');
    await expect(card.getByTestId('refused-reason')).toContainText('file_path_map');
    await expect(card.getByTestId('refused-reason')).not.toContainText('names no environments');
    await expect(card.getByRole('button')).not.toContainText(/Sync|Dry run/);

    for (const dryRun of [false, true]) {
      const response = await page.request.post(`/api/orchestrator/v1/ui/rules/${DR}/sync`, { data: { envs: ['production'], dryRun, prune: false } });
      expect(response.status(), `dryRun=${dryRun}`).toBe(409);
      expect(((await response.json()) as { detail: string }).detail).toMatch(/^gitea\/mongo-dr names no file to read/);
    }
    await expect.poll(async () => (await api.events(`&rule=${encodeURIComponent(DR)}`)).filter(e => e.reason === 'RuleRefused').length).toBeGreaterThan(0);
  } finally {
    await gitea.put(SCHEMA_REPO, 'bundle.json', world, 'ui-e2e: the rules, as they were', 'main');
    await stack.warm();
  }

  await rule.open(DR);
  await expect(rule.syncAll).toBeEnabled();
});

test('a rule that names its file is ordinary: no "names no file to read" anywhere, and a dry run of it ends in a log, not a failure', async ({ page, adminApi }) => {
  const S3 = 'gitea/s3';
  const view = await adminApi.rule(S3);
  expect((view as unknown as { refused?: string }).refused).toBeUndefined();
  const rule = new RulePage(page);
  await rule.open(S3);
  await expect(page.getByText('names no file to read')).toHaveCount(0);
  await rule.syncAll.click();
  await expect(rule.sync.dialog).toContainText('app-settings.yaml');
  await expect(rule.sync.dialog).not.toContainText('names no file to read');
  await rule.sync.cancel.click();
});

test('the suite starts with no leftover environment: setup forgot the ones an earlier run left', async ({ adminApi }) => {
  const leftovers = (await adminApi.rules()).flatMap(r => r.targets.filter(t => t.notFed).map(t => `${r.name} ${t.env}`));
  expect(leftovers).toEqual([]);
});

test('a push to a branch the rule does not map feeds no environment: the task says so, and nothing is written to a made-up one', async ({ adminApi, gitea, stack, stores }) => {
  const branch = unique('feature').toLowerCase();
  await gitea.ensureBranch(CONFIG_REPO, branch, 'develop');
  try {
    // gitea/redis-hash maps main and develop; this branch is neither
    const before = stores.redisKeys(2, 'ui-redis-hash-*').sort();
    const taskId = await stack.push('redis-hash', branch, await gitea.head(CONFIG_REPO, branch));
    const task = await adminApi.finished(taskId);
    expect(task.state, task.message).toBe('SUCCEEDED');
    expect(task.message).toBe(`nothing to do: branch ${branch} feeds no environment of gitea/redis-hash`);
    expect(stores.redisKeys(2, 'ui-redis-hash-*').sort(), 'nothing was written').toEqual(before);
    expect(stores.redisKeys(2, 'ui-redis-hash-default*'), 'nothing was written to a made-up environment').toEqual([]);
  } finally {
    await gitea.deleteBranch(CONFIG_REPO, branch);
  }
});

test('vt sync to an environment the rule does not produce is refused with the engine\'s words', async ({ page }) => {
  const token = await Gitea.tokenFor('demo-admin', 'VarTrack123');
  const response = await page.request.post('/api/gateway/v1/cli/sync', {
    headers: { Authorization: `Bearer ${token}` },
    data: { datasource: 'redis-hash', platform: 'gitea', env: 'default', file_path: 'app-settings.yaml', content: 'a: 1\n', dry_run: true, label: 'ui-e2e', tenant_id: 'demo' },
  });
  expect(response.status()).toBe(403);
  expect(await response.text()).toContain('nothing feeds default in the redis-hash rule');
});
