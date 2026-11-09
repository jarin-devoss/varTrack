import { expect, test, as } from '../fixtures';
import { CONFIG_REPO, Gitea } from '../fixtures/gitea';
import { PlaygroundPage } from '../pages/PlaygroundPage';

/**
 * The Playground: check a config file before pushing it. Paste, see the keys varTrack will extract, validate against the
 * rule's schema, dry-run the whole pipeline, or write the file to an environment (bypassing Git).
 * covers: route:/playground, action:playground.path, action:playground.datasource, action:playground.env, action:playground.content,
 * action:playground.validate, action:playground.dryrun, action:playground.sync, action:playground.sync.cancel, action:playground.copycli,
 * action:playground.task.details, state:playground.preview-error, state:playground.plaintext-secret, state:validation.ok,
 * state:validation.failed
 */
test.use(as('admin'));

const DATASOURCE = 'redis-string'; // strict_validation, so Validate has something to refuse
// The Playground goes through the gateway's CLI API, which judges an environment by the rule in the gateway's own configuration
// (config.cue): there redis-string maps develop and main, so `develop` is fed, and nothing feeds any other name.
const ENV = 'develop';
const NOT_FED = 'playground-e2e';
const KEYS = `ui-redis-string-${ENV}*`;

/** What the environment holds now: every key of it and its value. */
const snapshot = (stores: { redisKeys: (db: number, pattern: string) => string[]; redisGet: (db: number, key: string) => string }) =>
  Object.fromEntries(stores.redisKeys(3, KEYS).map(key => [key, stores.redisGet(3, key)]));

let valid = '';
let token = '';
test.beforeAll(async () => {
  const gitea = await Gitea.connect();
  try {
    valid = (await gitea.read(CONFIG_REPO, 'app-settings.yaml', 'develop'))!;
  } finally {
    await gitea.dispose();
  }
  token = await Gitea.tokenFor('demo-admin', 'VarTrack123');
});

/** Open the Playground with the demo file pasted and the admin's token given, as a person would before using it. */
async function ready(page: import('@playwright/test').Page, { withToken = true } = {}): Promise<PlaygroundPage> {
  const playground = new PlaygroundPage(page);
  await playground.open();
  await playground.datasource.selectOption(DATASOURCE);
  await playground.paste(valid, 'app-settings.yaml');
  if (withToken) await playground.useToken(token);
  return playground;
}

test.beforeEach(async ({ page }, info) => {
  if (!info.title.includes('[no token]')) await ready(page);
});

test('[no token] without a Git token, Validate says what is missing and where to put it', async ({ page }) => {
  const playground = await ready(page, { withToken: false });
  await expect(playground.tokenCard.getByText('Validate, Dry run and Sync need your Git token')).toBeVisible();
  await playground.validate.click();
  await expect(playground.error).toContainText('a Git token is required');
  await expect(playground.tokenCard.getByRole('textbox', { name: 'Token' })).toBeVisible();
});

test('[no token] the token box refuses more than one token, and accepts a Git personal access token', async ({ page }) => {
  const playground = await ready(page, { withToken: false });
  const box = playground.tokenCard.getByRole('textbox', { name: 'Token' });
  await box.fill('two tokens');
  await playground.tokenCard.getByRole('button', { name: 'Use this token' }).click();
  await expect(playground.tokenCard).toContainText("That isn't one token");

  await playground.useToken(token);
  await playground.validate.click();
  await expect(playground.validation).toContainText('Valid');
});

test('it opens with a sample file and lists the keys varTrack would extract, hiding what is a secret', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await page.goto('/playground');
  await expect(playground.filePath).toHaveValue('configs/app.yaml');
  await expect(playground.content).toHaveValue(/checkout/);
  await expect(playground.keys.getByRole('heading')).toHaveText('Keys varTrack will see (8)');
  await expect(playground.keys.getByRole('row').filter({ hasText: 'app.name' })).toContainText('checkout');
  await expect(playground.keys.getByRole('row').filter({ hasText: 'database.password' })).toContainText('resolved at sync time');
  await expect(playground.keys).not.toContainText('vault:checkout/db#password');
});

test('editing the file updates the keys, the format follows the extension, and a broken file says why', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await playground.paste('a:\n  b: "1"\n  c: "2"\n');
  await expect(playground.keys.getByRole('heading')).toHaveText('Keys varTrack will see (2)');
  await expect(page.getByText('YAML', { exact: true })).toBeVisible();

  await playground.paste('{"a": {"b": "1"}}', 'settings.json');
  await expect(page.getByText('JSON', { exact: true })).toBeVisible();
  await expect(playground.keys.getByRole('row').filter({ hasText: 'a.b' })).toContainText('1');

  await playground.paste('{"a": ', 'settings.json');
  await expect(page.getByText("Can't preview this file")).toBeVisible();

  await playground.paste('', 'settings.yaml');
  await expect(page.getByText('No keys yet')).toBeVisible();
});

test('a password written in plain text is flagged, and an @secret(...) is not', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await playground.paste('db:\n  password: hunter2\n  token: "@secret(vault:x#y)"\n');
  await expect(playground.keys.getByText(/look like credentials in plain text/)).toBeVisible();
  await expect(playground.keys.locator('code', { hasText: 'db.password' })).toBeVisible();
  await expect(playground.keys.locator('code', { hasText: 'db.token' })).toHaveCount(0);
});

test('Validate accepts the demo file against the rule\'s schema', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await playground.validate.click();
  await expect(playground.validation).toContainText('Valid');
  await expect(playground.validation).toContainText(/\d+ keys/);
});

test('Validate refuses a value the schema forbids, and says which', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await playground.paste(valid.replace(/log_level: \w+/, 'log_level: VERBOSE'));
  await playground.validate.click();
  await expect(playground.validation).toContainText('Invalid');
  await expect(playground.validation).toContainText('observability.log_level');
});

test('Dry run starts a task, follows it on the page, writes nothing, and Details opens the task', async ({ page, stores }) => {
  const before = snapshot(stores);
  const playground = new PlaygroundPage(page);
  await playground.environment.fill(ENV);
  await playground.paste(valid.replace(/log_level: \w+/, 'log_level: ERROR'));
  await playground.dryRun.click();

  const toast = page.getByRole('status').filter({ hasText: 'Dry run started' });
  await expect(toast).toBeVisible();
  await expect(playground.run).toContainText('Succeeded', { timeout: 30_000 });
  await expect(playground.run).toContainText('dry run');
  await expect(playground.run).toContainText(/\d+ keys parsed and validated/);
  expect(snapshot(stores), 'a dry run writes nothing').toEqual(before);

  await playground.run.getByRole('link', { name: 'Details' }).click();
  await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]{36}$/);
  await expect(page.getByText('Dry run').first()).toBeVisible();
});

test('Dry run of a file the schema refuses ends failed, and the page shows the reason', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await playground.environment.fill(ENV);
  await playground.paste(valid.replace(/log_level: \w+/, 'log_level: VERBOSE'));
  await playground.dryRun.click();
  await expect(playground.run).toContainText('Failed', { timeout: 30_000 });
  await expect(playground.run).toContainText(/log_level|strict_validation/);
});

test('an environment no branch feeds is refused with the reason, for a dry run and for Sync', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await playground.environment.fill(NOT_FED);
  await playground.dryRun.click();
  await expect(playground.error).toContainText(`nothing feeds ${NOT_FED} in the redis-string rule`);

  await playground.sync.click();
  await playground.confirm.getByRole('button', { name: `Sync to ${NOT_FED}` }).click();
  await expect(playground.error).toContainText(`nothing feeds ${NOT_FED} in the redis-string rule`);
});

test('Sync asks first, Cancel leaves everything as it was, and Sync writes the file to the environment', async ({ page, stores, adminApi }) => {
  const playground = new PlaygroundPage(page);
  const key = `ui-redis-string-${ENV}:observability_log_level`;
  const before = snapshot(stores);
  await playground.environment.fill(ENV);
  await playground.paste(valid.replace(/log_level: \w+/, 'log_level: WARN'));
  await expect(playground.sync).toHaveText(`Sync to ${ENV}`);

  await playground.sync.click();
  await expect(playground.confirm).toBeVisible();
  await expect(playground.confirm).toContainText('bypassing Git');
  await expect(playground.confirm).not.toContainText('looks like a production environment');
  await playground.confirm.getByRole('button', { name: 'Cancel' }).click();
  await expect(playground.confirm).toBeHidden();
  expect(snapshot(stores), 'Cancel writes nothing').toEqual(before);

  try {
    await playground.sync.click();
    await playground.confirm.getByRole('button', { name: `Sync to ${ENV}` }).click();
    await expect(page.getByRole('status').filter({ hasText: `Sync to ${ENV} started` })).toBeVisible();
    await expect(playground.run).toContainText('Succeeded', { timeout: 30_000 });
    await expect(playground.run).toContainText(/\d+ keys written/);
    expect(stores.redisGet(3, key)).toBe('WARN');
  } finally {
    // pasted, not pushed: put the environment back as Git says
    await adminApi.succeeded((await adminApi.sync('gitea/redis-string', [ENV]))[0]);
  }
});

test('an environment that looks like production gets a warning before the file is written', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await playground.environment.fill('production');
  await playground.sync.click();
  await expect(playground.confirm).toContainText('production looks like a production environment');
  await expect(playground.confirm).toContainText('Prefer a dry run first');
  await playground.confirm.getByRole('button', { name: 'Cancel' }).click();
});

test('the CLI command follows the form and can be copied', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const playground = new PlaygroundPage(page);
  await playground.environment.fill('staging');
  await expect(playground.cli).toHaveText(`vt sync --file app-settings.yaml --datasource ${DATASOURCE} --tenant demo --env staging --dry-run --wait`);

  await page.getByRole('button', { name: 'Copy command' }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`vt sync --file app-settings.yaml --datasource ${DATASOURCE} --tenant demo --env staging --dry-run --wait`);
});

test('the buttons are off until there is something to run', async ({ page }) => {
  const playground = new PlaygroundPage(page);
  await playground.paste('');
  await expect(playground.validate).toBeDisabled();
  await expect(playground.dryRun).toBeDisabled();
  await expect(playground.sync).toBeDisabled();
});
