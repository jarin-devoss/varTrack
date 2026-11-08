import { expect, test, as } from '../fixtures';
import { PEOPLE } from '../fixtures/roles';
import { Gitea } from '../fixtures/gitea';
import { PlaygroundPage } from '../pages/PlaygroundPage';
import { ShellPage } from '../pages/ShellPage';
import { SignInPage } from '../pages/SignInPage';

/**
 * Profile: who the person is signed in as, on which Git platforms, what they may do, the API token the Playground sends,
 * and their preferences. Signing out ends the session on the server, so that spec signs in again as its own session.
 * covers: route:/profile, action:profile.signout, action:profile.connect, action:profile.token.forget, action:profile.theme,
 * state:profile.admin, state:profile.reader, state:profile.no-token, state:profile.token
 */
const panel = (page: import('@playwright/test').Page, title: string) =>
  page.locator('section.panel').filter({ has: page.getByRole('heading', { name: title, exact: true }) });

test.describe('as admin', () => {
  test.use(as('admin'));

  test('says who is signed in, with which platform, and that an administrator sees every rule', async ({ page }) => {
    await page.goto('/profile');
    const hero = page.locator('.hero');
    await expect(hero.locator('.hero__kind')).toHaveText('Profile');
    await expect(hero.locator('.hero__name')).toContainText(/demo-admin|admin/i);
    await expect(hero.locator('.hero__meta')).toHaveText('@demo-admin on Gitea');

    const platforms = panel(page, 'Git platforms');
    await expect(platforms.getByText('signed in with')).toBeVisible();
    await expect(platforms.locator('dd.mono')).toHaveText('demo-admin');
    await expect(platforms).toContainText('You are a varTrack administrator: you see every rule.');
    await expect(platforms).toContainText(/Signed in .+ ago|Signed in just now|Signed in/);
  });
});

test.describe('as reader', () => {
  test.use(as('reader'));

  test('says each rule is shown when its own platform lets the account read it', async ({ page }) => {
    await page.goto('/profile');
    await expect(page.locator('.hero__meta')).toHaveText('@demo-viewer on Gitea');
    await expect(panel(page, 'Git platforms')).toContainText('Each rule is shown when its own platform lets your account there read one of its repositories');
    await expect(panel(page, 'Git platforms')).not.toContainText('You are a varTrack administrator');
  });

  test('before a token is given it says so and points to the Playground', async ({ page }) => {
    await page.goto('/profile');
    const token = panel(page, 'API token');
    await expect(token).toContainText('No token is set for this tab.');
    await expect(token).toContainText('You are signed in with your Git platform');
    await expect(token.getByRole('link', { name: 'Open the Playground' })).toHaveAttribute('href', '/playground');
    await expect(token.getByText("You're not signed in")).toHaveCount(0);
  });

  test('a token given to the Playground shows here, and Forget the token removes it', async ({ page }) => {
    const playground = new PlaygroundPage(page);
    await playground.open();
    await playground.useToken(await Gitea.tokenFor(PEOPLE.reader.login, PEOPLE.reader.password));

    // the token lives in the tab's memory: go by the UI's own links, not a new page load
    await new ShellPage(page).userMenu.click();
    await page.getByRole('menuitem', { name: 'Profile' }).click();
    await expect(page).toHaveURL(/\/profile$/);
    const token = panel(page, 'API token');
    await expect(token).toContainText('A token is set for this tab');
    await expect(token).toContainText('The Playground sends it');

    await token.getByRole('button', { name: 'Forget the token' }).click();
    await expect(token).toContainText('No token is set for this tab.');
    await expect(page).toHaveURL(/\/profile$/);
  });

  test('Theme here is the theme of the whole UI, and the page counts the favorites and live updates', async ({ page }) => {
    await page.goto('/profile');
    const preferences = panel(page, 'Preferences');
    await expect(preferences).toContainText('0 favorite rules · live updates on');

    await preferences.getByRole('group', { name: 'Theme' }).getByRole('button', { name: 'Dark' }).click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect(preferences.getByRole('group', { name: 'Theme' }).getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true');

    await page.getByRole('button', { name: 'Pause live updates' }).click();
    await expect(preferences).toContainText('live updates paused');
  });

  test('a favorite made on the Rules page is counted here', async ({ page }) => {
    await page.goto('/rules');
    await page.getByRole('button', { name: /^Add gitea\/redis-hash to favorites$/ }).click();
    await new ShellPage(page).userMenu.click();
    await page.getByRole('menuitem', { name: 'Profile' }).click();
    await expect(panel(page, 'Preferences')).toContainText('1 favorite rule · live updates on');
  });
});

test.describe('signing out', () => {
  test('Sign out of all platforms ends the session, and the next page asks to sign in', async ({ browser, baseURL }) => {
    // a session of its own: ending it must not end the one the other specs use
    const context = await browser.newContext({ baseURL });
    try {
      const page = await context.newPage();
      const signIn = new SignInPage(page);
      await signIn.open('/profile');
      await signIn.signInAs(PEOPLE.reader.login, PEOPLE.reader.password);
      await expect(page).toHaveURL(/\/profile$/);
      expect((await (await context.request.get('/api/orchestrator/v1/auth/session')).json()).authenticated).toBe(true);

      await page.getByRole('button', { name: 'Sign out of all platforms' }).click();
      await expect(page).toHaveURL(/\/login/);
      await signIn.ready();
      expect((await (await context.request.get('/api/orchestrator/v1/auth/session')).json()).authenticated).toBe(false);

      await page.goto('/rules');
      await expect(page).toHaveURL(/\/login/);
    } finally {
      await context.close();
    }
  });
});
