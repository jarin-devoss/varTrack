import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests of the web UI, against the real stack (e2e/docker-compose.yml): Gitea, the gateway, the
 * orchestrator, the watcher and the sinks. They sign in through Gitea's own page, as an admin, a writer, a reader
 * and an outsider, and use the UI the way people do. How to write one: e2e/README.md.
 *
 * The UI is reached at VT_UI_URL. In CI that is the UI image on :8080. Without it, the Vite dev server is started
 * on :8080 (the address the demo registers for sign-in) and proxies to the stack on localhost.
 */
const CI = !!process.env.CI;
const baseURL = process.env.VT_UI_URL ?? 'http://localhost:8080';

if (!CI && !process.env.VT_UI_URL) {
  // The dev server's proxy is not what ships, and it sometimes stalls a response on Windows; CI runs the UI image.
  console.warn(
    'e2e: VT_UI_URL is not set, so the Vite dev server is used. Set VT_UI_URL=http://localhost:8080 with the compose `ui` service ' +
      'running to test the UI as CI does (npm run test:e2e:stack does).',
  );
}

export default defineConfig({
  testDir: './e2e/specs',
  globalSetup: './e2e/global-setup.ts',
  // The specs share the demo stack's rules and sinks, so they run one after the other, in file order.
  fullyParallel: false,
  workers: 1,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 5_000 },
  reporter: [
    ['html', { open: 'never' }],
    ['json', { outputFile: 'test-results/results.json' }],
    CI ? ['github'] : ['list'],
  ],
  use: {
    baseURL,
    actionTimeout: 10_000,
    // The first attempt is the one that fails in a flake; the retry that passes has nothing to show.
    trace: 'retain-on-first-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer:
    CI || process.env.VT_UI_URL
      ? undefined
      : {
          command: 'npm run dev -- --host localhost --port 8080 --strictPort',
          url: baseURL,
          reuseExistingServer: true,
          timeout: 60_000,
        },
});
