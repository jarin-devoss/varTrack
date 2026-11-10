export interface FlakyTest {
  file: string;
  line: number;
  title: string;
}

/** The flaky tests of a Playwright JSON report. */
export function flakyTests(report: unknown): FlakyTest[];

/** The Markdown for the job summary. */
export function summary(report: unknown): string;
