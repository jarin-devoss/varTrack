import { expect, type Locator } from '@playwright/test';

/**
 * Show a tab, and return once it is showing: the click has landed (the tab is the selected one) and, when the tab's own
 * content is given, that content is there. A spec goes on from here with the tab on screen, never with a click still on its
 * way. *tabs* is the tab list (`getByRole('tablist', { name })`), so a tab of the same name in another list is not meant.
 *
 * It clicks once. A click the page drops is a bug in the page, and clicking again would hide it: the failure names the tab.
 */
export async function showTab(tabs: Locator, name: string, ready?: Locator): Promise<void> {
  const tab = tabs.getByRole('tab', { name, exact: true });
  await tab.click();
  await expect(tab, `the ${name} tab was clicked and is not selected`).toHaveAttribute('aria-selected', 'true');
  if (ready) await expect(ready, `the ${name} tab is selected and its content is not showing`).toBeVisible();
}
