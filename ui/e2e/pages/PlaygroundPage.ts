import { expect, type Locator, type Page } from '@playwright/test';

/** `/playground`: paste a config file, see its keys, validate it, dry-run it, or write it to an environment. */
export class PlaygroundPage {
  constructor(private readonly page: Page) {}

  get filePath() {
    return this.page.getByLabel('File path');
  }
  get datasource() {
    return this.page.getByLabel(/^Datasource/);
  }
  get environment() {
    return this.page.getByLabel(/^Environment/);
  }
  get content() {
    return this.page.getByLabel('Content');
  }
  get validate() {
    return this.page.getByRole('button', { name: /^(Validate|Validating…)$/ });
  }
  get dryRun() {
    return this.page.getByRole('button', { name: /^(Dry run|Starting…)$/ });
  }
  get sync() {
    return this.page.getByRole('button', { name: /^Sync to / }).first();
  }
  get confirm() {
    return this.page.getByRole('dialog', { name: /^Write this file to / });
  }
  get validation() {
    return this.page.locator('.card', { has: this.page.getByRole('heading', { name: 'Schema validation' }) });
  }
  get run() {
    return this.page.locator('.card', { has: this.page.getByRole('heading', { name: 'Pipeline run' }) });
  }
  get keys() {
    return this.page.locator('.card', { has: this.page.getByRole('heading', { name: /^Keys varTrack will see/ }) });
  }
  get cli() {
    return this.page.locator('.card', { has: this.page.getByRole('heading', { name: 'Same check from the CLI' }) }).locator('pre');
  }
  get error(): Locator {
    return this.page.locator('.card').getByRole('alert');
  }

  get tokenCard(): Locator {
    return this.page.locator('.card', { has: this.page.getByRole('heading', { name: 'Git token' }) });
  }

  /** Give the page a Git token, the way a person does: paste it into the card. It lives in the tab's memory only. */
  async useToken(token: string): Promise<void> {
    const box = this.tokenCard.getByRole('textbox', { name: 'Token' });
    if (!(await box.isVisible())) await this.tokenCard.getByText(/A token is set for this tab/).click();
    await box.fill(token);
    await this.tokenCard.getByRole('button', { name: 'Use this token' }).click();
    await expect(this.tokenCard.getByText(/A token is set for this tab/)).toBeVisible();
  }

  async open(): Promise<void> {
    await this.page.goto('/playground');
    await expect(this.page.getByRole('heading', { name: 'Playground' })).toBeVisible();
    // the datasources come from the bundle: until they arrive the field is a text box, then it is the real select
    await expect(this.page.locator('select').filter({ has: this.page.locator('option', { hasText: /^redis-string$/ }) })).toBeVisible();
  }

  /** Replace the pasted file. */
  async paste(content: string, filePath?: string): Promise<void> {
    if (filePath) await this.filePath.fill(filePath);
    await this.content.fill(content);
  }
}
