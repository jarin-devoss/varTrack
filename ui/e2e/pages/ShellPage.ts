import { expect, type Locator, type Page } from '@playwright/test';

/** The frame of every page: the section tabs, search, live updates, the Playground link, the user menu, toasts. */
export class ShellPage {
  constructor(private readonly page: Page) {}

  get brand() {
    return this.page.getByRole('link', { name: 'varTrack home' });
  }
  get sections() {
    return this.page.getByRole('navigation', { name: 'Sections' });
  }
  get liveToggle() {
    return this.page.getByRole('button', { name: /(pause|resume) live updates/i });
  }
  get playground() {
    return this.page.getByRole('link', { name: 'Playground' });
  }
  get userMenu() {
    return this.page.getByRole('button', { name: 'User menu' });
  }
  get search() {
    return this.page.getByRole('button', { name: /^search/i });
  }
  get toasts() {
    return this.page.getByRole('status');
  }

  /** The frame has mounted (so its keyboard shortcuts are listening) and the person is known. */
  async ready(): Promise<void> {
    await expect(this.userMenu).toBeVisible();
  }

  section = (label: string): Locator => this.sections.getByRole('link', { name: label, exact: true });

  async goTo(label: string): Promise<void> {
    await this.section(label).click();
  }

  /** The signed-in login the user menu shows once opened. */
  async openUserMenu(): Promise<void> {
    await this.userMenu.click();
  }

  async pauseLive(): Promise<void> {
    await this.page.getByRole('button', { name: 'Pause live updates' }).click();
    await expect(this.page.getByRole('button', { name: 'Resume live updates' })).toHaveAttribute('aria-pressed', 'false');
  }

  async resumeLive(): Promise<void> {
    await this.page.getByRole('button', { name: 'Resume live updates' }).click();
    await expect(this.page.getByRole('button', { name: 'Pause live updates' })).toHaveAttribute('aria-pressed', 'true');
  }
}
