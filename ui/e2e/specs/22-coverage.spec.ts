import { expect, test } from '@playwright/test';
import { staticProblems } from '../coverage/check';

/**
 * "Full" stays full: every route, named control and status of the UI is in e2e/coverage.json with the spec that covers it, and
 * every skipped test says why. (The same check runs in `npm test` without a stack; CI also checks, after the run, that each
 * spec the manifest relies on ran and passed: see e2e/README.md.)
 */
test('every route, named control and status of the UI has a spec, and no skip is silent', () => {
  expect(staticProblems()).toEqual([]);
});
