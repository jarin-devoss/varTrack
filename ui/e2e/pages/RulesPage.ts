import { expect, type Locator, type Page } from '@playwright/test';

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** `/rules`: every rule the signed-in person can see, as boxes, with filters. */
export class RulesPage {
  constructor(private readonly page: Page) {}

  get search() {
    return this.page.getByPlaceholder(/search rules/i);
  }
  get newRule() {
    return this.page.getByRole('button', { name: 'New rule' });
  }
  get clearFilters() {
    return this.page.getByRole('button', { name: 'Clear filters' });
  }
  get refresh() {
    return this.page.getByRole('button', { name: 'Refresh' });
  }
  get count() {
    return this.page.locator('.section-count');
  }

  filter = (name: 'Sync status' | 'Sink' | 'Environment' | 'Last sync' | 'Sort' | 'Tenant'): Locator =>
    this.page.getByLabel(name, { exact: true });

  /** The box of one rule, by its name (`gitea/redis-hash`). */
  card = (rule: string): Locator => this.page.getByRole('article', { name: new RegExp(`^${escape(rule)}:`) });
  cards = (): Locator => this.page.locator('article.rcard');

  async open(query = ''): Promise<void> {
    await this.page.goto(`/rules${query}`);
    await this.ready();
  }

  /** The rules are listed, or the empty state says there are none. */
  async ready(): Promise<void> {
    await expect(this.cards().first().or(this.page.getByRole('status').first())).toBeVisible();
  }

  async names(): Promise<string[]> {
    const labels = await this.cards().evaluateAll(nodes => nodes.map(n => n.getAttribute('aria-label') ?? ''));
    return labels.map(l => l.split(':')[0]).sort();
  }

  async openRule(rule: string): Promise<void> {
    await this.card(rule).getByRole('link', { name: rule }).first().click();
  }
}
