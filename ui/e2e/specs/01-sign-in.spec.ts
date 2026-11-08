import { expect, test, as, PEOPLE, unique } from '../fixtures';
import { SignInPage } from '../pages/SignInPage';
import { ShellPage } from '../pages/ShellPage';

/**
 * Signing in and out, through Gitea's own page. covers: route:/login, action:signin.gitea, action:signout
 */

test.describe('not signed in', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('any page sends you to the sign-in page, and back after signing in', async ({ page }) => {
    await page.goto('/rules');
    await expect(page).toHaveURL(/\/login\?return_url=%2Frules/);
    const signIn = new SignInPage(page);
    await signIn.ready();

    await signIn.withGitea.click();
    await signIn.signInOnGitea(PEOPLE.reader.login, PEOPLE.reader.password);
    await signIn.authorizeIfAsked();

    await expect(page).toHaveURL(/\/rules$/);
    await expect(new ShellPage(page).userMenu).toBeVisible();
  });

  test('the sign-in page says which platform it signs in with, and offers the API token for the Playground', async ({ page }) => {
    const signIn = new SignInPage(page);
    await signIn.open();
    await expect(page.getByText(/rules whose repositories gitea lets you read/i)).toBeVisible();
    await expect(signIn.apiToken).toBeVisible();
  });

  test('a wrong password stays on Gitea and shows its message', async ({ page }) => {
    const signIn = new SignInPage(page);
    await signIn.open();
    await signIn.withGitea.click();
    await signIn.signInOnGitea(PEOPLE.reader.login, 'not-the-password');
    await expect(page.getByText(/username or password is incorrect/i)).toBeVisible();
    expect(new URL(page.url()).origin).not.toBe(new URL(test.info().project.use.baseURL!).origin);
  });

  test('declining on the consent page returns to the sign-in page with a message', async ({ page, gitea }) => {
    // Gitea asks an account for consent once; an account that has not signed in yet is asked. Make one for this spec.
    const login = unique('consent').toLowerCase().replace(/[^a-z0-9-]/g, '-');
    await gitea.createUser(login, 'VarTrack123');
    try {
      const signIn = new SignInPage(page);
      await signIn.open();
      await signIn.withGitea.click();
      await signIn.signInOnGitea(login, 'VarTrack123');
      await page.getByRole('button', { name: /^cancel$/i }).click();
      await expect(page).toHaveURL(/\/login\?error=denied/);
      await expect(page.getByText('You declined access on the consent page.')).toBeVisible();
      await expect(new SignInPage(page).withGitea).toBeVisible();   // and the person can try again
    } finally {
      await gitea.deleteUser(login);
    }
  });

  test('a callback with a state this browser never started is refused', async ({ page }) => {
    // the server answers with a redirect to the sign-in page: wait for the redirect, not for the first response
    await page.goto('/api/orchestrator/v1/auth/callback?code=abc&state=forged', { waitUntil: 'commit' });
    await expect(page).toHaveURL(/\/login\?error=state/);
    await expect(page.getByText(/started in another browser or tab/i)).toBeVisible();
  });

  test('an unknown error code still shows a message', async ({ page }) => {
    await page.goto('/login?error=nonsense');
    await expect(page.getByText('Sign-in failed. Try again.')).toBeVisible();
  });
});

test.describe('signed in', () => {
  test.describe('with the saved session', () => {
    test.use(as('reader'));

    test('the sign-in page sends a signed-in person where they were headed', async ({ page }) => {
      await page.goto('/login?return_url=%2Fevents');
      await expect(page).toHaveURL(/\/events$/);
    });
  });

  test.describe('with a session of its own', () => {
    // Signing out ends a session on the server, so this one must not be the session the other specs share.
    test.use({ storageState: { cookies: [], origins: [] } });

    test('signing out ends the session on the server and returns to the sign-in page', async ({ page, api }) => {
      const signIn = new SignInPage(page);
      await signIn.open();
      await signIn.signInAs(PEOPLE.reader.login, PEOPLE.reader.password);
      expect((await api.session()).authenticated).toBe(true);

      const shell = new ShellPage(page);
      await shell.openUserMenu();
      await page.getByText('Sign out').click();

      await expect(page).toHaveURL(/\/login/);
      const after = await page.request.get('/api/orchestrator/v1/auth/session');
      expect(((await after.json()) as { authenticated: boolean }).authenticated).toBe(false);
      expect((await page.request.get('/api/orchestrator/v1/ui/rules')).status()).toBe(401);
    });
  });
});
