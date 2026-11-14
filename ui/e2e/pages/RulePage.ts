import { expect, type Locator, type Page } from '@playwright/test';
import { showTab } from './tabs';

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** The request that starts a sync: not the preview the panel makes to learn what a sync would delete. */
export const startedSync = (r: import('@playwright/test').Response): boolean =>
  /\/v1\/ui\/rules\/.+\/sync/.test(r.url()) && r.request().method() === 'POST' && !(r.request().postData() ?? '').includes('"preview":true');

/** The slide-over that starts a Sync or a Dry run. */
export class SyncPanel {
  constructor(private readonly page: Page) {}

  get dialog() {
    return this.page.getByRole('dialog').filter({ has: this.page.getByText(/^(Sync from Git|Dry run)$/) });
  }
  get dryRun() {
    return this.dialog.getByRole('checkbox', { name: /^Dry run/ });
  }
  get prune() {
    return this.dialog.getByRole('checkbox', { name: /Prune keys that are no longer in Git/ });
  }
  get selectAll() {
    return this.dialog.getByRole('button', { name: /^(Select all|Clear)$/ });
  }
  get cancel() {
    return this.dialog.getByRole('button', { name: 'Cancel' });
  }
  get start() {
    return this.dialog.getByRole('button', { name: /^(Sync \d+ environments?|Sync and delete \d+ keys?|Start dry run.*|Starting…)$/ });
  }

  // The log a dry run shows in the panel, in place of the form, and its footer.
  get log() {
    return this.dialog.getByTestId('dryrun-log');
  }
  /** The report of one environment's dry run. */
  report = (env: string): Locator => this.log.getByTestId('dryrun-report').filter({ has: this.page.getByText(env, { exact: true }) });
  /** The one-line outcome of a report. */
  get verdicts() {
    return this.log.getByTestId('dryrun-verdict');
  }
  get syncThese() {
    return this.dialog.getByRole('button', { name: 'Sync these environments' });
  }
  get runAgain() {
    return this.dialog.getByRole('button', { name: 'Run again' });
  }
  get back() {
    return this.dialog.getByRole('button', { name: 'Back', exact: true });
  }
  /** The footer's Close (the panel's own X, also named Close, comes after it). */
  get close() {
    return this.dialog.getByRole('button', { name: 'Close', exact: true }).first();
  }
  get error() {
    return this.dialog.getByRole('alert');
  }

  /** Press the primary button and return the tasks the orchestrator queued (one per environment). */
  async startAndCollect(): Promise<string[]> {
    const answered = this.page.waitForResponse(startedSync);
    await this.start.click();
    const response = await answered;
    expect(response.status(), 'the sync request is accepted').toBe(200);
    return ((await response.json()) as { taskIds: string[] }).taskIds;
  }

  environment = (env: string): Locator => this.dialog.getByRole('checkbox', { name: `Environment ${env}` });
}

/** `/rules/{platform}/{datasource}`: one rule, its environments, and the actions on them. */
export class RulePage {
  readonly sync: SyncPanel;

  constructor(private readonly page: Page) {
    this.sync = new SyncPanel(page);
  }

  get syncAll() {
    return this.page.locator('.action-bar').getByRole('button', { name: 'Sync', exact: true });
  }
  /** For someone who may sync: the button that opens the Sync panel ticked on Dry run. */
  get dryRunAll() {
    return this.page.locator('.action-bar').getByRole('button', { name: 'Dry run' });
  }
  get refresh() {
    return this.page.locator('.action-bar').getByRole('button', { name: 'Refresh' });
  }
  get star() {
    return this.page.getByRole('button', { name: /(add|remove).*favorite|star|pin/i }).first();
  }

  /** The box of one environment: `staging: In sync`, `main: OutOfSync`. */
  env = (env: string): Locator => this.page.getByRole('article', { name: new RegExp(`^${escape(env)}:`) });
  envSync = (env: string): Locator => this.env(env).getByRole('button', { name: `Sync ${env}` });
  /** What starts a dry run of one environment for someone who may not sync (their Sync is disabled, next to it). */
  envDryRun = (env: string): Locator => this.env(env).getByRole('button', { name: `Dry run ${env}` });
  /** The environment's own Diff: in every box, disabled (with the reason) when there is nothing to compare. */
  envDiff = (env: string): Locator => this.env(env).getByRole('button', { name: `Diff ${env}` });
  envDetails = (env: string): Locator => this.env(env).getByRole('button', { name: `Details for ${env}` });
  envNames = async (): Promise<string[]> => {
    const labels = await this.page.locator('article.envbox').evaluateAll(nodes => nodes.map(n => n.getAttribute('aria-label') ?? ''));
    return labels.map(l => l.split(':')[0]).sort();
  };

  async open(rule: string): Promise<void> {
    await this.page.goto(`/rules/${rule}`);
    await this.ready();
  }

  /** The Rule panel's tabs (Overview, History, Events, Definition), and the Environments panel's (Environments, Keys). */
  get ruleTabs() {
    return this.page.getByRole('tablist', { name: 'Rule', exact: true });
  }
  get environmentsTabs() {
    return this.page.getByRole('tablist', { name: 'Environments', exact: true });
  }
  async showRuleTab(name: 'Overview' | 'History' | 'Events' | 'Definition'): Promise<void> {
    await showTab(this.ruleTabs, name);
  }

  /** The Keys tab's search box: it is on the tab and on no other. */
  get keysSearch() {
    return this.page.locator('#environments-panel').getByPlaceholder('Search keys and values…');
  }
  /** The environment the Keys tab shows, as one option of its Environment switch. */
  keysEnvironment = (env: string): Locator =>
    this.page.locator('#environments-panel').getByRole('group', { name: 'Environment', exact: true }).getByRole('button', { name: env, exact: true });

  /** Click the Keys tab, and wait for it. A spec about the tab's content opens it with openKeys instead. */
  async showKeys(): Promise<void> {
    await showTab(this.environmentsTabs, 'Keys', this.keysSearch);
  }

  /** Open the rule on its Keys tab: the tab is in the address (`?tab=keys`), so there is no click to wait for. */
  async openKeys(rule: string): Promise<void> {
    await this.page.goto(`/rules/${rule}?tab=keys`);
    await expect(this.syncAll).toBeVisible();
    await expect(this.environmentsTabs.getByRole('tab', { name: 'Keys', exact: true })).toHaveAttribute('aria-selected', 'true');
    await expect(this.keysSearch).toBeVisible();
  }

  async ready(): Promise<void> {
    await expect(this.syncAll).toBeVisible();
    await expect(this.page.locator('article.envbox').first().or(this.page.getByText(/no environments|never been synced/i).first())).toBeVisible();
  }

  /** The toast after starting a sync: its title, and the task it links to. */
  async startedTask(): Promise<string> {
    const link = this.page.getByRole('status').getByRole('link', { name: /follow the task/i }).last();
    await expect(link).toBeVisible();
    const href = (await link.getAttribute('href')) ?? '';
    return href.split('/').pop() ?? '';
  }

  /**
   * Sync one environment from the Sync panel; returns the task it queued (the answer to the request, not a toast an earlier
   * sync left). A dry run does not close the panel: its log is read there, so the panel is closed once its log is shown.
   */
  async syncEnv(env: string, options: { dryRun?: boolean } = {}): Promise<string> {
    await this.envSync(env).click();
    await expect(this.sync.dialog).toBeVisible();
    if (options.dryRun !== undefined && (await this.sync.dryRun.count()) && (await this.sync.dryRun.isChecked()) !== options.dryRun) {
      await this.sync.dryRun.setChecked(options.dryRun);
    }
    const dry = !(await this.sync.dryRun.count()) || (await this.sync.dryRun.isChecked());
    const answered = this.page.waitForResponse(startedSync);
    await this.sync.start.click();
    if (!dry) {
      // A sync that will delete keys from a production-like environment asks to type "prune" first: a person types it.
      const confirm = this.page.getByRole('dialog', { name: 'Prune production keys?' });
      const toast = this.page.getByRole('status').getByRole('link', { name: /follow the task/i });
      await expect(confirm.or(toast.last())).toBeVisible();
      if (await confirm.isVisible()) {
        await confirm.getByRole('textbox').fill('prune');
        await confirm.getByRole('button', { name: 'Sync and prune' }).click();
      }
    }
    const response = await answered;
    expect(response.status(), 'the sync request is accepted').toBe(200);
    if (dry) {
      // The log is shown at once and fills in as the run moves (a run waiting for a worker says it is queued): closing it
      // before it ends is fine, the toast follows the run from there.
      await expect(this.sync.log).toBeVisible();
      await this.sync.close.click();
      await expect(this.sync.dialog).toBeHidden();
    }
    return ((await response.json()) as { taskIds: string[] }).taskIds[0];
  }
}
