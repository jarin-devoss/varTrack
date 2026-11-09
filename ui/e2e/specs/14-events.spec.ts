import { expect, test, as } from '../fixtures';
import { EventsPage } from '../pages/EventsPage';

/**
 * The Events page: what the product did, newest first. Every test makes the events it reads (a sync that works and one that
 * fails: `made`), and reads them as a reader.
 * covers: route:/events, action:events.filter.type, action:events.filter.reason, action:events.filter.rule, action:events.search,
 * action:events.group, action:events.clear, action:events.refresh, action:events.statusbar, action:events.row.open,
 * action:events.link.rule, action:events.link.task, state:events.empty-filter, state:event.warning, state:event.grouped
 */
/** Every kind of event, and what a person reads for it. */
const REASONS: [id: string, label: string][] = [
  ['WebhookReceived', 'Push received'],
  ['SyncStarted', 'Sync started'],
  ['SyncSucceeded', 'Synced'],
  ['SyncFailed', 'Sync failed'],
  ['ValidationFailed', 'Validation failed'],
  ['KeysPruned', 'Keys pruned'],
  ['PruneFailed', 'Prune failed'],
  ['PruneSkipped', 'Prune skipped'],
  ['ShapeChanged', 'Shape changed'],
  ['SharedKeySpace', 'Shared key space'],
  ['DriftDetected', 'Drift detected'],
  ['HealSucceeded', 'Healed'],
  ['HealFailed', 'Heal failed'],
  ['SchemaRefreshed', 'Schema refreshed'],
  ['RuleRefused', 'Rule refused'],
];

const RULE = 'gitea/redis-string'; // strict_validation, branch_as_env: develop reads app-settings.yaml on develop

test.describe('reading the events', () => {
  test.use(as('reader'));

  test.beforeEach(async ({ page, made }) => {
    await made.ok();
    await made.failed();
    await new EventsPage(page).open();
  });

  test('lists the events newest first, and the count says how many', async ({ page, api }) => {
    const events = new EventsPage(page);
    const shown = await api.events('&limit=500');
    expect(shown.length).toBeGreaterThan(0);
    // repeats are folded: every row is one distinct (reason, rule, environment, message)
    const distinct = new Set(shown.map(e => `${e.reason}|${e.rule ?? ''}|${e.env ?? ''}|${e.message}`)).size;
    await expect(events.rows).toHaveCount(Math.min(distinct, 50));
    await expect(events.count).toContainText(`${shown.length} events`);
    const times = shown.map(e => e.time);
    expect(times, 'the API lists newest first').toEqual([...times].sort().reverse());
  });

  test('the Type filter keeps only warnings, the status bar says how many, and both live in the URL', async ({ page, api }) => {
    const events = new EventsPage(page);
    const warnings = (await api.events('&limit=500')).filter(e => e.type === 'Warning');
    expect(warnings.length).toBeGreaterThan(0);

    await events.filter('Type').selectOption('Warning');
    await expect(page).toHaveURL(/[?&]type=Warning/);
    await expect(events.count).toContainText(`${warnings.length} events`);
    await expect(events.rows.locator('.pill', { hasText: 'Warning' })).toHaveCount(await events.rows.count());

    await events.clearFilters.click();
    await expect(page).not.toHaveURL(/type=/);
    await page.locator('.status-chart__legend').getByRole('button', { name: /^Warning/ }).click();
    await expect(page).toHaveURL(/[?&]type=Warning/);
  });

  test('the Reason filter keeps one kind of event, and the options are worded for people', async ({ page, api }) => {
    const events = new EventsPage(page);
    const synced = (await api.events('&limit=500')).filter(e => e.reason === 'SyncSucceeded');
    expect(synced.length).toBeGreaterThan(0);
    await events.filter('Reason').selectOption({ label: 'Synced' });
    await expect(page).toHaveURL(/[?&]reason=SyncSucceeded/);
    await expect(events.count).toContainText(`${synced.length} events`);
    for (const headline of await events.headlines()) expect(headline).toBe('Synced');
  });

  test('the Reason filter offers every kind of event in words, and each kind that has happened lists', async ({ page, api }) => {
    const events = new EventsPage(page);
    const options = await events.filter('Reason').locator('option').allInnerTexts();
    expect(options).toEqual(['Any reason', ...REASONS.map(([, label]) => label)]);

    const happened = new Set((await api.events('&limit=500')).map(e => e.reason));
    expect(happened.size, 'the stack has made several kinds of event').toBeGreaterThan(3);
    for (const [id, label] of REASONS) {
      if (!happened.has(id)) continue;
      await events.open(`?reason=${id}`);
      await expect(events.filter('Reason').locator('option:checked')).toHaveText(label);
      await expect(events.rows.first()).toBeVisible();
      for (const headline of await events.headlines()) expect(headline).toBe(label);
    }
  });

  test('the Rule filter keeps the events of one rule', async ({ page, api }) => {
    const events = new EventsPage(page);
    const ofRule = (await api.events('&limit=500')).filter(e => e.rule === RULE);
    expect(ofRule.length).toBeGreaterThan(0);
    await events.filter('Rule').selectOption(RULE);
    await expect(page).toHaveURL(/[?&]rule=gitea%2Fredis-string/);
    await expect(events.count).toContainText(`${ofRule.length} events`);
    await expect(events.rows.locator('.rowx__title a').first()).toContainText(/redis-string|develop/);
  });

  test('searching matches the message, and says so when nothing matches', async ({ page }) => {
    const events = new EventsPage(page);
    await events.search.fill('strict_validation');
    await expect(events.rows.first()).toBeVisible();
    await expect(page).toHaveURL(/[?&]q=strict_validation/);

    await events.search.fill('nothing was ever said like this');
    await expect(page.getByText('No events match')).toBeVisible();
    await expect(events.rows).toHaveCount(0);

    await events.clearFilters.click();
    await expect(events.rows.first()).toBeVisible();
    await expect(events.search).toHaveValue('');
  });

  test('Group repeats folds identical events into one row with a count, and turning it off lists each one', async ({ page, api, tasks }) => {
    const events = new EventsPage(page);
    await tasks.ok();      // the same sync again: the same "Sync started" and "Synced" said twice
    await events.refresh.click();
    const all = await api.events('&limit=500');
    const distinct = new Set(all.map(e => `${e.reason}|${e.rule ?? ''}|${e.env ?? ''}|${e.message}`)).size;
    expect(distinct, 'an event has repeated').toBeLessThan(all.length);

    await expect(events.groupRepeats).toBeChecked();
    await expect(events.rows.locator('.tag', { hasText: /^×\d+$/ }).first()).toBeVisible();
    await events.groupRepeats.click(); // its state lives in the URL: it changes after the router has, so check it with a retrying assertion
    await expect(events.groupRepeats).not.toBeChecked();
    await expect(page).toHaveURL(/[?&]group=off/);
    await expect(events.rows).toHaveCount(Math.min(all.length, 50));
    await expect(events.rows.locator('.tag', { hasText: /^×\d+$/ })).toHaveCount(0);
  });

  test('a row opens to its message, time, rule and task, and the links go there', async ({ page, api }) => {
    const events = new EventsPage(page);
    await events.filter('Rule').selectOption(RULE);
    await events.filter('Reason').selectOption({ label: 'Synced' });
    const row = events.rows.first();
    await row.locator('.rowx__toggle').click();
    const details = row.locator('.rowx__details');
    await expect(details.getByText('Message', { exact: true })).toBeVisible();
    await expect(details.getByText('Rule', { exact: true })).toBeVisible();
    await expect(details.getByRole('link', { name: RULE })).toHaveAttribute('href', `/rules/demo/${RULE}`);

    const task = details.getByRole('link', { name: /^[0-9a-f-]{36}$/ });
    await expect(task).toBeVisible();
    const taskId = (await task.textContent())!;
    expect((await api.task(taskId)).state).toBe('SUCCEEDED');
    await task.click();
    await expect(page).toHaveURL(new RegExp(`/tasks/${taskId}`));
  });

  test('the name of an event opens the environment it was about', async ({ page }) => {
    const events = new EventsPage(page);
    await events.filter('Rule').selectOption(RULE);
    await events.rows.first().locator('.rowx__title a').click();
    await expect(page).toHaveURL(/\/rules\/demo\/gitea\/redis-string(\?env=\w+)?/);
  });

  test('Refresh asks the server again', async ({ page }) => {
    const events = new EventsPage(page);
    const asked = page.waitForRequest(r => /\/v1\/ui\/events/.test(r.url()) && r.method() === 'GET');
    await events.refresh.click();
    await asked;
    await expect(events.rows.first()).toBeVisible();
  });
});
