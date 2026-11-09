import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The coverage manifest (coverage.json) lists every route, every named control and every status value of the UI, with the
 * spec that covers it, so that "the whole UI is tested" stays true. A new screen, button or status without an entry fails
 * `staticProblems()`; an entry whose spec did not pass in a run fails `resultProblems()`.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
export const UI = path.resolve(here, '..', '..');
const SPECS = path.join(UI, 'e2e', 'specs');

/** One thing the UI has, and what covers it: a spec, or the reason nothing can. */
export interface Entry {
  /** The spec file, without `.spec.ts`. */
  spec?: string;
  /** Text the spec (or the page objects it uses) must contain; the entry's own name when absent. */
  contains?: string;
  /** Why no end-to-end spec covers it (what covers it instead). */
  reason?: string;
}

export interface Manifest {
  routes: Record<string, Entry>;
  labels: Record<string, Entry>;
  states: Record<string, Entry>;
}

const read = (...parts: string[]): string => fs.readFileSync(path.join(UI, ...parts), 'utf8');

export function manifest(): Manifest {
  return JSON.parse(read('e2e', 'coverage.json')) as Manifest;
}

function walk(dir: string, accept: (file: string) => boolean, into: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, accept, into);
    else if (accept(full)) into.push(full);
  }
  return into;
}

/** The paths of `<Route path="…">` in App.tsx. */
export function sourceRoutes(): string[] {
  return [...read('src', 'App.tsx').matchAll(/<Route\s+path="([^"]+)"/g)].map(m => m[1]);
}

/** The literal `aria-label="…"` of every control and landmark in the source. */
export function sourceLabels(): string[] {
  const files = walk(path.join(UI, 'src'), f => f.endsWith('.tsx'));
  const labels = new Set<string>();
  for (const file of files) for (const m of fs.readFileSync(file, 'utf8').matchAll(/aria-label="([^"]+)"/g)) labels.add(m[1]);
  return [...labels].sort();
}

/** The keys of `export const NAME: Record<…> = { KEY: …, }` (top-level keys of that object). */
function recordKeys(file: string, name: string): string[] {
  const text = read(...file.split('/'));
  const start = text.indexOf(`export const ${name}:`);
  if (start < 0) throw new Error(`${file} has no ${name}`);
  const body = text.slice(text.indexOf('= {', start) + 3, text.indexOf('\n};', start));
  return [...body.matchAll(/^ {2}([A-Za-z_]+):/gm)].map(m => m[1]);
}

/** Every status value the UI words differently, as `group.value`. */
export function sourceStates(): string[] {
  const groups: [string, string, string][] = [
    ['sync', 'src/lib/status.ts', 'SYNC'],
    ['drift', 'src/lib/status.ts', 'DRIFT'],
    ['task', 'src/lib/status.ts', 'TASK'],
    ['file', 'src/lib/status.ts', 'FILE'],
    ['trigger', 'src/lib/status.ts', 'TRIGGER'],
    ['event', 'src/features/events.tsx', 'REASON'],
  ];
  return groups.flatMap(([group, file, name]) => recordKeys(file, name).map(key => `${group}.${key}`));
}

const specFiles = (): string[] => fs.readdirSync(SPECS).filter(f => f.endsWith('.spec.ts'));

/** The text of one spec and of the page objects and fixtures it can use (not of the other specs). */
function evidenceFor(spec: string): string {
  const shared = ['pages', 'fixtures'].flatMap(dir => walk(path.join(UI, 'e2e', dir), f => f.endsWith('.ts')));
  return [path.join(SPECS, `${spec}.spec.ts`), ...shared].map(f => fs.readFileSync(f, 'utf8')).join('\n').toLowerCase();
}

export function staticProblems(): string[] {
  const problems: string[] = [];
  const m = manifest();

  const same = (what: string, fromSource: string[], listed: string[]) => {
    for (const x of fromSource) if (!listed.includes(x)) problems.push(`${what} "${x}" is in the source and not in coverage.json: add it with the spec that covers it`);
    for (const x of listed) if (!fromSource.includes(x)) problems.push(`${what} "${x}" is in coverage.json and no longer in the source: remove it`);
  };
  same('route', [...new Set(sourceRoutes())], Object.keys(m.routes));
  same('label', sourceLabels(), Object.keys(m.labels));
  same('state', sourceStates(), Object.keys(m.states));

  const specs = new Set(specFiles().map(f => f.replace(/\.spec\.ts$/, '')));
  for (const [kind, entries] of Object.entries(m) as [string, Record<string, Entry>][]) {
    for (const [name, entry] of Object.entries(entries)) {
      if (entry.reason) {
        if (entry.spec) problems.push(`${kind} "${name}" has a spec and a reason: pick one`);
        continue;
      }
      if (!entry.spec) {
        problems.push(`${kind} "${name}" has neither a spec nor a reason`);
        continue;
      }
      if (!specs.has(entry.spec)) {
        problems.push(`${kind} "${name}" names ${entry.spec}, which is not a spec in e2e/specs`);
        continue;
      }
      const needle = (entry.contains ?? name.replace(/^[a-z]+\./, '')).toLowerCase();
      if (!evidenceFor(entry.spec).includes(needle)) problems.push(`${kind} "${name}" is covered by ${entry.spec}, but nothing in e2e mentions "${needle}"`);
    }
  }

  // a skip needs its reason
  for (const file of specFiles()) {
    const source = fs.readFileSync(path.join(SPECS, file), 'utf8');
    for (const skip of source.matchAll(/\b(?:test|it)\.(?:skip|fixme)\(([^)]*)\)/g)) {
      if (!/['"`]/.test(skip[1])) problems.push(`${file} skips a test without saying why: ${skip[0]}`);
    }
  }
  return problems;
}

interface JsonTest {
  status: string;
  annotations?: { type: string; description?: string }[];
  results: { status: string }[];
}
interface JsonSuite {
  file?: string;
  suites?: JsonSuite[];
  specs?: { file: string; title: string; tests: JsonTest[] }[];
}

/** What a run did, per spec file, from Playwright's JSON report. */
export function resultProblems(resultsPath: string): string[] {
  const problems: string[] = [];
  const report = JSON.parse(fs.readFileSync(resultsPath, 'utf8')) as { suites: JsonSuite[] };
  const byFile = new Map<string, { passed: number; bad: string[] }>();
  let total = 0;

  const visit = (suite: JsonSuite) => {
    for (const spec of suite.specs ?? []) {
      const entry = byFile.get(spec.file) ?? { passed: 0, bad: [] };
      byFile.set(spec.file, entry);
      for (const test of spec.tests) {
        total++;
        const last = test.results[test.results.length - 1]?.status ?? test.status;
        if (test.status === 'skipped') {
          const why = test.annotations?.find(a => a.type === 'skip' || a.type === 'fixme')?.description;
          if (!why) entry.bad.push(`${spec.title}: skipped without a reason`);
        } else if (last === 'passed' || test.status === 'expected' || test.status === 'flaky') {
          entry.passed++;
        } else {
          entry.bad.push(`${spec.title}: ${last}`);
        }
      }
    }
    suite.suites?.forEach(visit);
  };
  report.suites.forEach(visit);

  if (total === 0) problems.push('the run executed no tests');
  const m = manifest();
  const needed = new Set(
    [...Object.values(m.routes), ...Object.values(m.labels), ...Object.values(m.states)].flatMap(e => (e.spec ? [`${e.spec}.spec.ts`] : [])),
  );
  for (const file of [...needed].sort()) {
    const ran = byFile.get(file);
    if (!ran) problems.push(`${file} covers entries of coverage.json and did not run`);
    else {
      if (ran.passed === 0) problems.push(`${file} covers entries of coverage.json and no test of it passed`);
      for (const bad of ran.bad) problems.push(`${file}: ${bad}`);
    }
  }
  return problems;
}
