// Which tests of a Playwright run failed an attempt and passed a retry.
//
// A retry keeps the job green, so a flake shows only as a notice inside it. This lists the flaky tests as Markdown, for the
// job's summary page, next to the report that holds each failed attempt's screenshot, video and trace:
//
//   node e2e/flaky-summary.mjs test-results/results.json >> "$GITHUB_STEP_SUMMARY"
//
// It never fails the job: a missing or unreadable report prints nothing, and says why on stderr.
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

/** The flaky tests of a Playwright JSON report: where each is, and what it is called. */
export function flakyTests(report) {
  const found = [];
  const visit = (suite, titles) => {
    for (const spec of suite.specs ?? []) {
      if ((spec.tests ?? []).some(test => test.status === 'flaky')) {
        found.push({ file: spec.file, line: spec.line, title: [...titles, spec.title].join(' › ') });
      }
    }
    for (const child of suite.suites ?? []) visit(child, [...titles, child.title]);
  };
  // A report's first level is the spec files: a file's name is the location, not part of a test's title.
  for (const file of report.suites ?? []) visit(file, []);
  return found;
}

/** The Markdown for the job summary. */
export function summary(report) {
  const flaky = flakyTests(report);
  if (!flaky.length) return 'No flaky browser tests.\n';
  return [
    `### Flaky browser tests (${flaky.length})`,
    '',
    "Each failed an attempt and passed its retry, so the job is green. The failed attempt's screenshot, video and trace are in the `ui-e2e-report` artifact.",
    '',
    ...flaky.map(t => `- \`${t.file}:${t.line}\` — ${t.title}`),
    '',
  ].join('\n');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [file] = process.argv.slice(2);
  try {
    process.stdout.write(summary(JSON.parse(fs.readFileSync(file, 'utf8'))));
  } catch (error) {
    console.error(`no flaky-test summary: ${file ?? '(no report named)'} cannot be read: ${error.message}`);
  }
}
