import { expect, test, as } from '../fixtures';
import { RulesPage } from '../pages/RulesPage';
import { ShellPage } from '../pages/ShellPage';

/**
 * Starred rules. They are kept in the browser (local storage), so they survive a reload and are the person's own.
 * covers: route:/favorites, action:rule.star, action:rule.unstar, state:favorites.empty
 */
test.use(as('reader'));

const RULE = 'gitea/redis-hash';
const OTHER = 'gitea/zookeeper';

test.beforeEach(async ({ page }) => {
  // a clean browser: no stars from an earlier spec
  await page.addInitScript(() => {
    if (!sessionStorage.getItem('vt-e2e-cleared')) {
      localStorage.clear();
      sessionStorage.setItem('vt-e2e-cleared', '1');
    }
  });
});

const star = (page: import('@playwright/test').Page, rule: string) =>
  page.getByRole('button', { name: new RegExp(`(Add|Remove) ${rule} (to|from) favorites`) });

test('with none starred, the page says so and points to the rules', async ({ page }) => {
  await page.goto('/favorites');
  await expect(page.getByText('No favorites yet')).toBeVisible();
  await page.getByRole('link', { name: 'Browse rules' }).click();
  await expect(page).toHaveURL(/\/rules$/);
});

test('starring a rule on the Rules page puts it on Favorites, and unstarring takes it off', async ({ page }) => {
  const rules = new RulesPage(page);
  await rules.open();
  await star(page, RULE).click();
  await expect(star(page, RULE)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: `Remove ${RULE} from favorites` })).toBeVisible();

  await new ShellPage(page).goTo('Favorites');
  await expect(rules.card(RULE)).toBeVisible();
  await expect(rules.cards()).toHaveCount(1);

  await star(page, RULE).click();
  await expect(page.getByText('No favorites yet')).toBeVisible();
});

test('stars survive a reload and belong to each rule separately', async ({ page }) => {
  const rules = new RulesPage(page);
  await rules.open();
  await star(page, RULE).click();
  await star(page, OTHER).click();
  await page.reload();
  await rules.ready();
  await expect(star(page, RULE)).toHaveAttribute('aria-pressed', 'true');
  await expect(star(page, OTHER)).toHaveAttribute('aria-pressed', 'true');

  await star(page, OTHER).click();
  await page.reload();
  await rules.ready();
  await expect(star(page, RULE)).toHaveAttribute('aria-pressed', 'true');
  await expect(star(page, OTHER)).toHaveAttribute('aria-pressed', 'false');
});

test('the Rule page has the same star', async ({ page }) => {
  await page.goto(`/rules/${RULE}`);
  await expect(page.getByRole('button', { name: new RegExp(`Add ${RULE} to favorites`) })).toBeVisible();
  await page.getByRole('button', { name: new RegExp(`Add ${RULE} to favorites`) }).click();
  await page.goto('/favorites');
  await expect(new RulesPage(page).card(RULE)).toBeVisible();
});

test.describe('a person who can see none of the rules', () => {
  test.use(as('outsider'));

  test('a rule they starred before losing access is not shown', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('vartrack.prefs.v1', JSON.stringify({ pinned: ['demo/gitea/redis-hash'] }));
    });
    await page.goto('/favorites');
    expect(await page.evaluate(() => localStorage.getItem('vartrack.prefs.v1'))).toContain('redis-hash');   // it really is starred
    await expect(page.getByText('No favorites yet')).toBeVisible();
    await expect(new RulesPage(page).cards()).toHaveCount(0);
  });
});
