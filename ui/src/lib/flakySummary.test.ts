import { describe, expect, it } from 'vitest';
import { flakyTests, summary } from '../../e2e/flaky-summary.mjs';
// A report Playwright wrote (retries: 1, the JSON reporter), cut down to what the summary reads: two tests that failed their
// first attempt and passed the retry, one at the top of the file and one in a describe, and two that passed.
import written from './__fixtures__/playwright-flaky-report.json';

const test = (status: string) => ({ status, results: [] });

describe('the flaky tests of a run', () => {
  it('lists a test that failed an attempt and passed a retry, with its place and its whole title', () => {
    expect(flakyTests(written)).toEqual([
      { file: 'example.spec.ts', line: 8, title: 'flaky on its first attempt' },
      { file: 'example.spec.ts', line: 14, title: 'a group › flaky inside a describe' },
    ]);
  });

  it('does not list a test that passed, failed or was skipped', () => {
    const clean = { suites: [{ title: 'a', file: 'a', specs: ['expected', 'unexpected', 'skipped'].map((s, i) => ({ title: s, file: 'a', line: i, tests: [test(s)] })) }] };
    expect(flakyTests(clean)).toEqual([]);
    expect(summary(clean)).toBe('No flaky browser tests.\n');
  });

  it('says in the summary how many there are and where the evidence is', () => {
    const text = summary(written);
    expect(text).toContain('### Flaky browser tests (2)');
    expect(text).toContain('- `example.spec.ts:14` — a group › flaky inside a describe');
    expect(text).toContain('`ui-e2e-report` artifact');
  });

  it('reads an empty report as no flakes', () => {
    expect(flakyTests({})).toEqual([]);
  });
});
