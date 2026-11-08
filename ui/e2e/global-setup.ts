import fs from 'node:fs';
import path from 'node:path';
import { chromium, expect, type FullConfig } from '@playwright/test';
import { PEOPLE, ROLES, type Person } from './fixtures/roles';
import { buildWorld } from './fixtures/world';

/**
 * Sign in once per role, through the real thing: the UI's "Sign in with Gitea", Gitea's own login form, and its
 * consent page the first time an account signs in. The browser state is saved and reused by the specs, so a spec
 * starts signed in as the role it names and never repeats this.
 */
async function signIn(baseURL: string, person: Person): Promise<void> {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ baseURL });
    const page = await context.newPage();
    const here = new URL(baseURL).origin;

    await page.goto('/login');
    await page.getByRole('link', { name: /sign in with gitea/i }).click();

    // Gitea's login form.
    await page.locator('#user_name').fill(person.login);
    await page.locator('#password').fill(person.password);
    await page.getByRole('button', { name: /^sign in$/i }).click();

    // Gitea asks an account to authorize the application once; after that it goes straight back.
    await page.waitForURL(url => url.origin === here || url.pathname.includes('/login/oauth/authorize'));
    if (new URL(page.url()).origin !== here) {
      await page.getByRole('button', { name: /authorize application/i }).click();
    }
    await page.waitForURL(url => url.origin === here && !url.pathname.startsWith('/login'));

    const session = await context.request.get('/api/orchestrator/v1/auth/session');
    const body = (await session.json()) as { authenticated: boolean; user?: { login: string } };
    expect(body.authenticated, `${person.login} is signed in`).toBe(true);
    expect(body.user?.login).toBe(person.login);

    fs.mkdirSync(path.dirname(person.storageState), { recursive: true });
    await context.storageState({ path: person.storageState });
  } finally {
    await browser.close();
  }
}

export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0].use.baseURL ?? 'http://localhost:8080';
  for (const role of ROLES) await signIn(baseURL, PEOPLE[role]);
  await buildWorld(baseURL);
}
