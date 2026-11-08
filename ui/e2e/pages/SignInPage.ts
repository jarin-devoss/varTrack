import { expect, type Page } from '@playwright/test';

/** `/login`: the page people land on when they are not signed in. */
export class SignInPage {
  constructor(private readonly page: Page) {}

  get heading() {
    return this.page.getByRole('heading', { name: /sign in to vartrack/i });
  }
  get withGitea() {
    return this.page.getByRole('link', { name: /sign in with gitea/i });
  }
  get apiToken() {
    return this.page.getByText('API token for the Playground');
  }

  async open(returnUrl?: string): Promise<void> {
    await this.page.goto(returnUrl ? `/login?return_url=${encodeURIComponent(returnUrl)}` : '/login');
    await this.ready();
  }

  async ready(): Promise<void> {
    await expect(this.heading).toBeVisible();
    await expect(this.withGitea).toBeVisible();
  }

  /** Gitea's own login form (this is not varTrack's page). */
  async signInOnGitea(login: string, password: string): Promise<void> {
    await this.page.locator('#user_name').fill(login);
    await this.page.locator('#password').fill(password);
    await this.page.getByRole('button', { name: /^sign in$/i }).click();
  }

  /** The whole thing a person does: the button, Gitea's form, the consent page when asked. Ends on the UI. */
  async signInAs(login: string, password: string): Promise<void> {
    await this.withGitea.click();
    await this.signInOnGitea(login, password);
    await this.authorizeIfAsked();
    await this.page.waitForURL(url => !url.pathname.startsWith('/login') && url.origin === new URL(this.page.url()).origin);
  }

  /** Gitea asks for consent the first time an account signs in to an application. */
  async authorizeIfAsked(): Promise<void> {
    const authorize = this.page.getByRole('button', { name: /authorize application/i });
    if (await authorize.isVisible().catch(() => false)) await authorize.click();
  }
}
