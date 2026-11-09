import { expect, test, as, type Role } from '../fixtures';

/**
 * Every link a person can follow goes somewhere that works. The spec walks the UI from the home page the way a person
 * clicks through it, following each internal link once, and fails on a page that is not found, throws, logs an error, or
 * calls the server and gets an error back that the page did not expect. Then the pages that are meant to say "not found".
 * covers: route:*, route:/rules/:tenant/:platform/:datasource, state:notfound.page, state:notfound.rule, state:notfound.task,
 * action:notfound.home, action:notfound.rules
 */
const PAGES_LIMIT = 80;

for (const role of ['admin', 'reader'] as Role[]) {
  test.describe(`as ${role}`, () => {
    test.use(as(role));

    test('every internal link reachable from Home opens a page that works', async ({ page }) => {
      test.setTimeout(240_000);
      const problems: string[] = [];
      let at = '';
      page.on('pageerror', e => problems.push(`${at}: uncaught ${e.message}`));
      page.on('console', m => {
        if (m.type() === 'error') problems.push(`${at}: console error ${m.text()}`);
      });
      page.on('requestfailed', r => problems.push(`${at}: request failed ${r.url()} (${r.failure()?.errorText})`));
      page.on('response', r => {
        const url = new URL(r.url());
        // 404 for a task that is not recorded yet is part of how the page waits for one
        if (r.status() >= 400 && url.pathname.startsWith('/api/') && !/\/v1\/ui\/tasks\//.test(url.pathname)) {
          problems.push(`${at}: ${r.request().method()} ${url.pathname} answered ${r.status()}`);
        }
      });

      const seen = new Set<string>();
      const queue = ['/home'];
      const visited: string[] = [];
      while (queue.length && visited.length < PAGES_LIMIT) {
        const path = queue.shift()!;
        if (seen.has(path)) continue;
        seen.add(path);
        at = path;
        await page.goto(path);
        await expect(page.locator('main')).toBeVisible();
        // the page is done loading when its loading rows are gone (the pages poll, so the network never goes quiet)
        await expect(page.getByLabel('Loading')).toHaveCount(0);
        visited.push(path);

        await expect(page.getByText('Page not found'), `${path} is a page`).toHaveCount(0);
        await expect(page.getByText(/^No rule named/), `${path} names a rule that exists`).toHaveCount(0);
        await expect(page.locator('main').getByText(/Couldn't load/), `${path} loads`).toHaveCount(0);

        // the user menu holds links the pages do not (Profile, Settings): open it once
        if (path === '/home') await page.getByRole('button', { name: 'User menu' }).click();
        const hrefs = await page.locator('a[href^="/"]').evaluateAll(links => links.map(a => a.getAttribute('href') ?? ''));
        if (path === '/home') await page.keyboard.press('Escape');
        for (const href of hrefs) {
          const normal = href.split('#')[0];
          if (!normal || seen.has(normal) || /^\/(login|api)\b/.test(normal)) continue;
          queue.push(normal);
        }
      }

      expect(problems, `problems on ${visited.length} pages`).toEqual([]);
      // the walk reached each kind of page
      for (const kind of [/^\/rules$/, /^\/rules\/demo\/gitea\/.+/, /\?env=/, /^\/environments/, /^\/events/, /^\/tasks$/, /^\/tasks\/[0-9a-f-]{36}/, /^\/playground/, /^\/settings/, /^\/profile/]) {
        expect(visited.some(v => kind.test(v)), `the walk reached ${kind}`).toBe(true);
      }
    });
  });
}

test.describe('pages that say there is nothing here', () => {
  test.use(as('admin'));

  test('an address nothing serves says so and offers Home', async ({ page }) => {
    await page.goto('/there/is/nothing/here');
    await expect(page).toHaveTitle('Not found · varTrack');
    await expect(page.getByText('Page not found')).toBeVisible();
    await expect(page.getByText('There is nothing at')).toContainText('/there/is/nothing/here');
    await page.getByRole('link', { name: 'Go home' }).click();
    await expect(page).toHaveURL(/\/home$/);
  });

  test('a rule that does not exist says so and offers the list of rules', async ({ page }) => {
    await page.goto('/rules/demo/gitea/no-such-datasource');
    await expect(page.getByText('No rule named gitea/no-such-datasource')).toBeVisible();
    await page.getByRole('link', { name: 'All rules' }).click();
    await expect(page).toHaveURL(/\/rules$/);
  });

  test('a task that does not exist says so and offers the list of tasks', async ({ page }) => {
    await page.goto('/tasks/ffffffff-ffff-ffff-ffff-ffffffffffff');
    await expect(page.getByText('Task not found')).toBeVisible();
    await page.getByRole('link', { name: 'All tasks' }).click();
    await expect(page).toHaveURL(/\/tasks$/);
  });

  test('the root address goes to Home', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/home$/);
  });
});
