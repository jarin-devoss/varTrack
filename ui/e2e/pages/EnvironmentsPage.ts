import { expect, type Locator, type Page } from '@playwright/test';
import { SyncPanel } from './RulePage';

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** `/environments`: every environment of every rule, one row each. */
export class EnvironmentsPage {
  readonly sync: SyncPanel;

  constructor(private readonly page: Page) {
    this.sync = new SyncPanel(page);
  }

  get search() {
    return this.page.getByPlaceholder(/search environments/i);
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
  get rows(): Locator {
    return this.page.locator('.rowx');
  }

  filter = (name: 'Sync status' | 'Sink' | 'Environment' | 'Tenant'): Locator => this.page.getByLabel(name, { exact: true });

  /** The row of one environment of one rule. */
  row = (rule: string, env: string): Locator =>
    this.rows.filter({ has: this.page.getByRole('button', { name: new RegExp(`details for ${escape(rule)} ${escape(env)}$`) }) });

  /** The button that opens (or closes) a row's details. */
  toggle = (rule: string, env: string): Locator =>
    this.page.getByRole('button', { name: new RegExp(`details for ${escape(rule)} ${escape(env)}$`) });

  async open(query = ''): Promise<void> {
    await this.page.goto(`/environments${query}`);
    await this.ready();
  }

  /** The rows are listed, or the empty state says nothing matches. */
  async ready(): Promise<void> {
    await expect(this.rows.first().or(this.page.getByRole('status').first())).toBeVisible();
  }

  /** "rule env" of every listed row, in the order shown. */
  async listed(): Promise<string[]> {
    const labels = await this.rows.locator('.rowx__toggle').evaluateAll(nodes => nodes.map(n => n.getAttribute('aria-label') ?? ''));
    return labels.map(l => l.replace(/^(Show|Hide) details for /, ''));
  }
}
