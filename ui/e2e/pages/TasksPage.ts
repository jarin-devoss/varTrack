import { expect, type Locator, type Page } from '@playwright/test';

/** `/tasks`: every sync the product ran, newest first. */
export class TasksPage {
  constructor(private readonly page: Page) {}

  get search() {
    return this.page.getByPlaceholder(/search task id/i);
  }
  get clearFilters() {
    return this.page.getByRole('button', { name: 'Clear filters' });
  }
  get count() {
    return this.page.locator('.section-count');
  }
  get rows(): Locator {
    return this.page.locator('.rowx');
  }
  get lookup() {
    return this.page.getByRole('search', { name: 'Open a task by ID' });
  }
  get lookupInput() {
    return this.lookup.getByRole('textbox');
  }
  get lookupOpen() {
    return this.lookup.getByRole('button', { name: 'Open' });
  }

  filter = (name: 'State' | 'Trigger' | 'Tenant'): Locator => this.page.getByLabel(name, { exact: true });

  /** The row of one task, by the first eight characters of its ID. */
  row = (taskId: string): Locator => this.rows.filter({ has: this.page.getByRole('button', { name: `Show details for task ${taskId.slice(0, 8)}` }).or(this.page.getByRole('button', { name: `Hide details for task ${taskId.slice(0, 8)}` })) });

  toggle = (taskId: string): Locator => this.rows.getByRole('button', { name: new RegExp(`details for task ${taskId.slice(0, 8)}$`) });

  async open(query = ''): Promise<void> {
    await this.page.goto(`/tasks${query}`);
    await this.ready();
  }

  async ready(): Promise<void> {
    await expect(this.rows.first().or(this.page.getByRole('status').first())).toBeVisible();
  }

  /** The eight-character IDs of the tasks listed, in the order shown. */
  async listed(): Promise<string[]> {
    const labels = await this.rows.locator('.rowx__toggle').evaluateAll(nodes => nodes.map(n => n.getAttribute('aria-label') ?? ''));
    return labels.map(l => l.replace(/^(Show|Hide) details for task /, ''));
  }
}
