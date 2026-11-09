import { expect, type Locator, type Page } from '@playwright/test';

/** `/settings`: where the UI gets its data, the services behind it, what the bundle holds, and how it looks. */
export class SettingsPage {
  constructor(private readonly page: Page) {}

  /** One section, by its title ("Connection", "Services", "Datasources", "Platforms, secrets and schemas", "Appearance"). */
  panel = (title: string): Locator => this.page.locator('section.panel').filter({ has: this.page.getByRole('heading', { name: title, exact: true }) });

  /** The button that folds a section. */
  fold = (title: string): Locator => this.panel(title).locator('button.panel__header');

  get gatewayUrl() {
    return this.panel('Connection').getByLabel(/^Gateway URL/);
  }
  get orchestratorUrl() {
    return this.panel('Connection').getByLabel(/^Orchestrator URL/);
  }
  get watcherUrl() {
    return this.panel('Connection').getByLabel(/^Watcher admin URL/);
  }
  get test() {
    return this.panel('Connection').getByRole('button', { name: /^(Test connection|Testing…)$/ });
  }
  get save() {
    return this.panel('Connection').getByRole('button', { name: 'Save' });
  }
  get discard() {
    return this.panel('Connection').getByRole('button', { name: 'Discard' });
  }
  get theme() {
    return this.panel('Appearance').getByRole('group', { name: 'Theme' });
  }
  get live() {
    return this.panel('Appearance').getByRole('checkbox', { name: /Live updates/ });
  }
  get reset() {
    return this.panel('Appearance').getByRole('button', { name: 'Reset favorites and preferences' });
  }

  async open(path = '/settings'): Promise<void> {
    await this.page.goto(path);
    await expect(this.page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();
  }
}
