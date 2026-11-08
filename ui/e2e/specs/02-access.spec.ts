import { expect, test, as, PEOPLE, type Role } from '../fixtures';
import { RulesPage } from '../pages/RulesPage';

/**
 * Who sees what. A rule, environment or task a person cannot see answers "not found", exactly as one that does not
 * exist does, so the answer never reveals that something is there. covers: state:access.admin, state:access.reader,
 * state:access.outsider, state:hidden-rules, state:not-found.rule, state:not-found.task
 */

const SEEN_BY_ALL = ['gitea/redis-hash', 'gitea/redis-string', 'gitea/zookeeper', 'gitea/mongo-document', 'gitea/mongo', 'gitea/mongo-primary'];

for (const role of ['admin', 'writer', 'reader'] as Role[]) {
  test.describe(`${PEOPLE[role].login} (${role})`, () => {
    test.use(as(role));

    test('sees the rules whose repository the account can read: the Rules page lists what the API lists', async ({ page, api }) => {
      const names = (await api.rules()).map(r => r.name).sort();
      expect(names).toEqual(expect.arrayContaining(SEEN_BY_ALL));
      const rules = new RulesPage(page);
      await rules.open();
      expect(await rules.names()).toEqual(names);
    });

    test('the session says who they are and whether they are an admin', async ({ api }) => {
      const session = await api.session();
      expect(session.authenticated).toBe(true);
      expect(session.user?.login).toBe(PEOPLE[role].login);
      expect(!!session.admin).toBe(role === 'admin');
    });
  });
}

test.describe('the outsider (an account that can read none of the repositories)', () => {
  test.use(as('outsider'));

  test('sees no rule, and nothing says a private repository has any', async ({ page, api }) => {
    expect(await api.rules()).toEqual([]);
    const rules = new RulesPage(page);
    await page.goto('/rules');
    // Signed in to gitea, so nothing is "hidden because you are not connected": rules in repositories the account
    // cannot read are not even counted, which would show that a private repository has rules.
    await expect(page.getByText('No rules in the bundle')).toBeVisible();
    await expect(page.getByText(/hidden/)).toHaveCount(0);
    const text = await page.locator('main').innerText();
    for (const name of SEEN_BY_ALL) expect(text, `${name} is not named`).not.toContain(name.split('/')[1]);
    await expect(rules.cards()).toHaveCount(0);
  });

  test('every list is empty: Environments, Events, Tasks, Favorites, Home', async ({ page, api }) => {
    expect((await api.tasks()).length).toBe(0);
    for (const path of ['/environments', '/events', '/tasks']) {
      await page.goto(path);
      await expect(page.locator('article.rcard, article.envbox')).toHaveCount(0);
    }
    await page.goto('/home');
    await expect(page.getByText('0 rules · 0 environments')).toBeVisible();
  });

  test('a rule that exists answers exactly as a rule that does not', async ({ page, api }) => {
    const real = await page.request.get('/api/orchestrator/v1/ui/rules/gitea/redis-hash');
    const fake = await page.request.get('/api/orchestrator/v1/ui/rules/gitea/no-such-rule');
    expect(real.status()).toBe(404);
    expect(fake.status()).toBe(404);
    // the same answer, the rule's name being the only thing that differs
    const normalize = async (r: typeof real, name: string) => JSON.stringify(await r.json()).replace(name, '<rule>');
    expect(await normalize(real, 'redis-hash')).toBe(await normalize(fake, 'no-such-rule'));
    expect(await api.session().then(s => s.authenticated)).toBe(true);

    await page.goto('/rules/gitea/redis-hash');
    await expect(page.getByText('No rule named gitea/redis-hash')).toBeVisible();
    await page.goto('/rules/gitea/no-such-rule');
    await expect(page.getByText('No rule named gitea/no-such-rule')).toBeVisible();
  });

  test('an environment of a rule they cannot see, and its keys, answer not found', async ({ page }) => {
    for (const path of ['/v1/ui/rules/gitea/redis-hash/targets/staging/keys', '/v1/ui/rules/gitea/redis-hash/sync']) {
      const response = path.endsWith('/sync')
        ? await page.request.post(`/api/orchestrator${path}`, { data: { envs: ['staging'], dryRun: true, prune: false } })
        : await page.request.get(`/api/orchestrator${path}`);
      expect(response.status(), path).toBe(404);
    }
  });

  test('a task they cannot see answers as a task that does not exist', async ({ page, adminApi }) => {
    const [someTask] = await adminApi.tasks();
    const real = await page.request.get(`/api/orchestrator/v1/ui/tasks/${someTask.taskId}`);
    const fake = await page.request.get('/api/orchestrator/v1/ui/tasks/00000000-0000-4000-8000-000000000000');
    expect(real.status()).toBe(fake.status());
    expect(real.status()).toBe(404);

    await page.goto(`/tasks/${someTask.taskId}`);
    await expect(page.getByText('Task not found')).toBeVisible();
  });

  test('the tenant filter does not appear: there is nothing to filter', async ({ page }) => {
    await page.goto('/rules');
    await expect(new RulesPage(page).filter('Tenant')).toHaveCount(0);
  });
});

test.describe('the admin sees everything', () => {
  test.use(as('admin'));

  test('at least what any other person sees, and the session says admin', async ({ api, page }) => {
    const mine = (await api.rules()).map(r => r.name);
    const reader = await page.context().request.get('/api/orchestrator/v1/ui/rules');
    expect(reader.status()).toBe(200);
    for (const name of SEEN_BY_ALL) expect(mine).toContain(name);
    expect((await api.session()).admin).toBe(true);
  });
});
