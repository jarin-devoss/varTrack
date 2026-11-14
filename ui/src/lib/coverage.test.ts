import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
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

  it('fails a skipped test, with or without a reason, in any spec file', () => {
    const skipped = (title: string, annotations: object[] = []) => ({ title, file: 'zz-not-in-the-manifest.spec.ts', line: 1, tests: [{ status: 'skipped', annotations, results: [{ status: 'skipped' }] }] });
    const file = path.join(os.tmpdir(), `vt-results-${process.pid}.json`);
    fs.writeFileSync(file, JSON.stringify({ suites: [{ file: 'zz-not-in-the-manifest.spec.ts', specs: [skipped('says why', [{ type: 'skip', description: 'nothing needs attention' }]), skipped('says nothing')] }] }));
    try {
      const problems = resultProblems(file);
      expect(problems).toContain('zz-not-in-the-manifest.spec.ts: says why: skipped (nothing needs attention)');
      expect(problems).toContain('zz-not-in-the-manifest.spec.ts: says nothing: skipped without a reason');
    } finally {
      fs.rmSync(file, { force: true });
    }
  });

  const results = process.env.COVERAGE_RESULTS;
  it.skipIf(!results)('relies only on specs that ran and passed', () => {
    expect(resultProblems(results as string)).toEqual([]);
  });
});
