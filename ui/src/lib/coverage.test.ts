import { describe, expect, it } from 'vitest';
import { resultProblems, staticProblems } from '../../e2e/coverage/check';

/**
 * The end-to-end suite must keep covering the whole UI (e2e/coverage.json). This runs without a stack: a new route, a new
 * named control or a new status that the manifest does not list fails here, before anyone has to run the browser tests.
 * With COVERAGE_RESULTS=test-results/results.json (CI sets it after the Playwright run) it also checks that every spec the
 * manifest relies on ran and passed.
 */
describe('the end-to-end coverage manifest', () => {
  it('lists every route, named control and status of the UI, each with a spec that mentions it', () => {
    expect(staticProblems()).toEqual([]);
  });

  const results = process.env.COVERAGE_RESULTS;
  it.skipIf(!results)('relies only on specs that ran and passed', () => {
    expect(resultProblems(results as string)).toEqual([]);
  });
});
