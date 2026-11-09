import { expect, type Locator, type Page } from '@playwright/test';

/** `/tasks/{id}`: one sync, its pipeline, its facts and its files. */
export class TaskPage {
  constructor(private readonly page: Page) {}

  get hero(): Locator {
    return this.page.locator('.hero');
  }
  get kind(): Locator {
    return this.page.locator('.hero__kind');
  }
  get name(): Locator {
    return this.page.locator('.hero__name');
  }
  get stages(): Locator {
    return this.page.getByLabel('Pipeline stages');
  }
  get copyId(): Locator {
    return this.page.getByRole('button', { name: 'Copy task ID' });
  }
  get files(): Locator {
    return this.page.locator('.table-wrap');
  }

  /** The value shown next to one label in the Pipeline facts ("Task ID", "Source", "Result", …). */
  fact = (label: string): Locator => this.page.locator('dl.facts > div').filter({ has: this.page.locator('dt', { hasText: new RegExp(`^${label}$`) }) }).locator('dd');

  async open(taskId: string): Promise<void> {
    await this.page.goto(`/tasks/${taskId}`);
    await this.ready();
  }

  /** The task is shown, or the page says it is not found. */
  async ready(): Promise<void> {
    await expect(this.hero.or(this.page.getByText('Task not found'))).toBeVisible();
  }
}
