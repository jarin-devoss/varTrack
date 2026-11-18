// The browser suite, run the way CI runs it, in one command:
//
//   0. the demo tour (demo/README.md, "The tour"), on a demo of its own started from demo/ and taken down again;
//   1. the stack on clean volumes (e2e/docker-compose.yml: the demo, with the UI image on :8080, and the regression on it) and its 26-suite regression;
//   2. the browser tests against that UI;
//   3. the coverage check on what they did (every spec the manifest relies on ran and passed).
//
//   npm run test:e2e:stack               everything
//   npm run test:e2e:stack -- --no-up    the stack is already up (a stack you brought up another way)
//   npm run test:e2e:stack -- --keep     leave the stack running afterwards
//   npm run test:e2e:stack -- --no-tour  skip the tour (it starts a demo of its own first)
//   npm run test:e2e:stack -- e2e/specs/11-prune.spec.ts   only those specs (the coverage check is then skipped)
//
// Run it in the foreground or as a harness background task: a command started detached from the shell is killed with it.
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ui = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const e2e = path.resolve(ui, '..', 'e2e');
const demo = path.resolve(ui, '..', 'demo');
const args = process.argv.slice(2);
const flags = new Set(args.filter(a => a.startsWith('--')));
const specs = args.filter(a => !a.startsWith('--'));
const shell = process.platform === 'win32';

function run(label, command, commandArgs, options = {}) {
  console.log(`\n── ${label} ──`);
  const result = spawnSync(command, commandArgs, { stdio: 'inherit', shell, ...options });
  return result.status ?? 1;
}

let failed = 0;
if (!flags.has('--no-up') && !flags.has('--no-tour')) {
  // The tour is about the demo as a person runs it (webhooks on, nothing a suite wrote), so it has a demo of its own, from demo/,
  // under its own project name; the e2e stack below is a stack of its own, and shares no file with it.
  const downDemo = () => run('Clearing the demo', 'docker', ['compose', 'down', '-v', '--remove-orphans'], { cwd: demo });
  downDemo();
  const tourEnv = { ...process.env, VT_UI_URL: process.env.VT_UI_URL ?? 'http://localhost:8080', VT_TOUR_PROJECT: 'vartrack-demo' };
  const tour =
    run('Starting the demo', 'docker', ['compose', 'up', '--build', '--detach'], { cwd: demo }) === 0 &&
    run('Seeding the demo', 'docker', ['compose', 'wait', 'demo-seed'], { cwd: demo }) === 0 &&
    run('The demo tour', 'npm', ['run', 'test:e2e:tour'], { cwd: ui, env: tourEnv }) === 0;
  downDemo();
  if (!tour) {
    console.error('The demo tour failed: the stack is not brought up.');
    process.exit(1);
  }
}
if (!flags.has('--no-up')) {
  const down = () => run('Clearing the stack', 'docker', ['compose', 'down', '-v', '--remove-orphans'], { cwd: e2e });
  down();
  if (run('Bringing the stack up', 'docker', ['compose', 'up', '--build', '--detach'], { cwd: e2e }) !== 0) process.exit(1);
  if (run('The 26-suite regression', 'docker', ['compose', 'wait', 'e2e-regression'], { cwd: e2e }) !== 0) {
    console.error('The regression failed: the browser suite is not run on a stack that is not sound.');
    process.exit(1);
  }
} else if (!process.env.VT_ALLOW_STALE_IMAGES) {
  // A stack brought up without --build reuses images of any age, and a UI newer than its orchestrator shows a blank page.
  if (run('Are the stack\'s images as new as the code', 'node', ['e2e/stale-images.mjs'], { cwd: path.resolve(ui, '..') }) === 1) {
    console.error('The stack is older than this checkout: the browser suite is not run on it (VT_ALLOW_STALE_IMAGES=1 to run anyway).');
    process.exit(1);
  }
}

const env = { ...process.env, VT_UI_URL: process.env.VT_UI_URL ?? 'http://localhost:8080' };
if (run('The browser tests', 'npx', ['playwright', 'test', ...specs], { cwd: ui, env }) !== 0) failed = 1;

if (specs.length === 0) {
  if (
    run('The coverage check', 'npx', ['vitest', 'run', 'src/lib/coverage.test.ts'], {
      cwd: ui,
      env: { ...env, COVERAGE_RESULTS: 'test-results/results.json' },
    }) !== 0
  ) {
    failed = 1;
  }
}

if (!flags.has('--keep') && !flags.has('--no-up') && failed === 0) {
  run('Taking the stack down', 'docker', ['compose', 'down', '-v', '--remove-orphans'], { cwd: e2e });
}
process.exit(failed);
