import { expect, type Locator, type Page } from '@playwright/test';

/** `/events`: what happened, newest first, with repeats folded into one row. */
export class EventsPage {
  constructor(private readonly page: Page) {}

  get search() {
    return this.page.getByPlaceholder(/search messages/i);
  }
  get groupRepeats() {
    return this.page.getByRole('checkbox', { name: 'Group repeats' });
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

  filter = (name: 'Type' | 'Reason' | 'Rule' | 'Tenant'): Locator => this.page.getByLabel(name, { exact: true });

  async open(query = ''): Promise<void> {
    await this.page.goto(`/events${query}`);
    await this.ready();
  }

  /** Events are listed, or the empty state says there are none. */
  async ready(): Promise<void> {
    await expect(this.rows.first().or(this.page.getByRole('status').first())).toBeVisible();
  }

  /** The "what happened" of every listed row ("Synced", "Sync failed", …), in the order shown. */
  async headlines(): Promise<string[]> {
    return this.rows.locator('.rowx__title > span.rowx__name').allInnerTexts();
  }
}
