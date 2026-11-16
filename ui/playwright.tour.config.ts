import { defineConfig, devices } from '@playwright/test';

/**
 * The demo tour (demo/README.md, "The tour"): every step of it, done on a freshly seeded demo, reading the README for what to run.
 * It has no world and no retries: a step that fails once has a README or a product to fix.
 *
 *   VT_UI_URL            the demo's UI (default http://localhost:8080)
 *   VT_TOUR_PROJECT      the Compose project the demo runs as, for the README's `docker compose exec` lines
 *                        (default vartrack-demo: what `docker compose up` in demo/ names it)
 *   VT_BASH              the shell that runs the README's commands (default bash)
 */
const baseURL = process.env.VT_UI_URL ?? 'http://localhost:8080';

export default defineConfig({
  testDir: './e2e/tour',
  globalSetup: './e2e/tour/global-setup.ts',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['json', { outputFile: 'test-results/tour.json' }], ...(process.env.CI ? [['github'] as ['github']] : [])],
  use: {
    baseURL,
    actionTimeout: 10_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
