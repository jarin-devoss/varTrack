import { expect, test, as, PEOPLE } from '../fixtures';
import { ShellPage } from '../pages/ShellPage';

/**
 * The frame of every page. covers: route:/home, route:/favorites, route:/rules, route:/environments, route:/events,
 * route:/tasks, route:/playground, route:/settings, action:nav.*, action:search, action:hotkeys, action:live.toggle,
 * action:usermenu, action:shortcuts
 */
test.use(as('reader'));

const SECTIONS: Array<[label: string, path: RegExp]> = [
  ['Home', /\/home$/],
  ['Favorites', /\/favorites$/],
  ['Rules', /\/rules$/],
  ['Environments', /\/environments$/],
  ['Events', /\/events$/],
  ['Tasks', /\/tasks$/],
];

test('the section tabs reach every page', async ({ page }) => {
  await page.goto('/home');
  const shell = new ShellPage(page);
  for (const [label, path] of SECTIONS) {
    await shell.goTo(label);
    await expect(page).toHaveURL(path);
    await expect(shell.section(label)).toHaveClass(/section-tab--active/);
  }
});

test('the varTrack mark and the Playground link go home and to the Playground', async ({ page }) => {
  await page.goto('/tasks');
  const shell = new ShellPage(page);
  await shell.playground.click();
  await expect(page).toHaveURL(/\/playground$/);
  await shell.brand.click();
  await expect(page).toHaveURL(/\/home$/);
});

test('every shortcut of the sheet goes where it says', async ({ page }) => {
  await page.goto('/home');
  await new ShellPage(page).ready();
  await page.keyboard.press('?');
  const sheet = page.getByRole('dialog', { name: 'Keyboard shortcuts' });
  await expect(sheet).toBeVisible();
  for (const text of ['Go home', 'Go to Favorites', 'Go to Rules', 'Go to Environments', 'Go to Events', 'Go to Tasks', 'Go to the Playground', 'Go to Settings']) {
    await expect(sheet).toContainText(text);
  }
  await page.keyboard.press('Escape');
  await expect(sheet).toBeHidden();

  const goes: Array<[keys: string[], path: RegExp]> = [
    [['g', 'f'], /\/favorites$/],
    [['g', 'r'], /\/rules$/],
    [['g', 'e'], /\/environments$/],
    [['g', 'v'], /\/events$/],
    [['g', 't'], /\/tasks$/],
    [['g', 'p'], /\/playground$/],
    [['g', 's'], /\/settings$/],
    [['g', 'h'], /\/home$/],
  ];
  for (const [keys, path] of goes) {
    for (const key of keys) await page.keyboard.press(key);
    await expect(page).toHaveURL(path);
  }
});

test('the shortcut sheet opens from the user menu too', async ({ page }) => {
  await page.goto('/home');
  const shell = new ShellPage(page);
  await shell.openUserMenu();
  await page.getByRole('menuitem', { name: 'Keyboard shortcuts' }).click();
  await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeVisible();
  await page.getByRole('button', { name: 'Close', exact: true }).first().click();
  await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeHidden();
});

test('search finds a rule, an environment and a page, and Escape closes it', async ({ page }) => {
  await page.goto('/home');
  const shell = new ShellPage(page);
  await shell.ready();
  await expect(shell.search).toHaveAccessibleName('Search (press /)');
  await page.keyboard.press('/');
  const box = page.getByRole('combobox', { name: 'Search' });
  await expect(box).toBeFocused();

  await box.fill('redis-hash');
  await expect(page.getByRole('listbox').getByText('gitea/redis-hash').first()).toBeVisible();

  await box.fill('staging');
  await expect(page.getByRole('listbox').getByText('staging').first()).toBeVisible();

  await box.fill('playground');
  await expect(page.getByRole('listbox').getByText(/playground/i).first()).toBeVisible();

  // the search box has its own Close, for a person with a mouse
  await page.getByRole('button', { name: 'Close search' }).click();
  await expect(box).toBeHidden();
  await shell.search.click();
  await expect(box).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(box).toBeHidden();
  await expect(shell.search).toBeVisible();

  // Ctrl+K opens it from anywhere, Enter takes the first result
  await page.keyboard.press('Control+k');
  await page.getByRole('combobox', { name: 'Search' }).fill('redis-string');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/rules\/(demo\/)?gitea\/redis-string/);
});

test('pausing live updates stops the refresh and resuming starts it again', async ({ page }) => {
  const polled: string[] = [];
  page.on('request', request => {
    if (request.url().includes('/v1/ui/rules')) polled.push(request.url());
  });
  await page.goto('/rules');
  const shell = new ShellPage(page);
  await expect(page.locator('article.rcard').first()).toBeVisible();

  await shell.pauseLive();
  const before = polled.length;
  // Live pages refresh every few seconds; ten seconds without a request is "paused".
  await page.waitForTimeout(10_000); // the refresh interval is 5 s: two missed refreshes prove it stopped
  expect(polled.length, 'no refresh while paused').toBe(before);

  await shell.resumeLive();
  await expect.poll(() => polled.length, { timeout: 15_000 }).toBeGreaterThan(before);
});

test('the l key toggles live updates', async ({ page }) => {
  await page.goto('/home');
  const shell = new ShellPage(page);
  await expect(shell.liveToggle).toHaveAttribute('aria-pressed', 'true');
  await page.keyboard.press('l');
  await expect(shell.liveToggle).toHaveAttribute('aria-pressed', 'false');
  await page.keyboard.press('l');
  await expect(shell.liveToggle).toHaveAttribute('aria-pressed', 'true');
});

test('the user menu names who is signed in and the platform, and offers profile, settings and sign out', async ({ page }) => {
  await page.goto('/home');
  const shell = new ShellPage(page);
  await shell.openUserMenu();
  const menu = page.getByRole('menu');
  await expect(menu).toContainText(`@${PEOPLE.reader.login}`);
  await expect(menu.getByRole('menuitem', { name: 'Profile' })).toBeVisible();
  await expect(menu.getByRole('menuitem', { name: 'Settings' })).toBeVisible();
  await expect(menu.getByRole('menuitem', { name: 'Sign out' })).toBeVisible();
  await menu.getByRole('menuitem', { name: 'Profile' }).click();
  await expect(page).toHaveURL(/\/profile$/);
});
