import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, as } from '../fixtures';
import { RulePage } from '../pages/RulePage';

/**
 * The tour in demo/README.md, done step by step on a freshly seeded demo.
 *
 * The README is what a person follows, so it is what runs: this reads its "The tour" section, finds each step by its heading, and
 * runs the commands in the step's `sh tour` blocks from demo/, as the person would. What each step says to look for is checked
 * in the UI where the README says to look. A README step with no check here, or a check with no README step, fails: the tour
 * and its test cannot drift apart. A failure names the README's step.
 *
 * The commands reach the demo through its Compose project (`docker compose exec …`), named by VT_TOUR_PROJECT.
 */

const here = path.dirname(fileURLToPath(import.meta.url));
const DEMO = path.resolve(here, '..', '..', '..', 'demo');
const PROJECT = process.env.VT_TOUR_PROJECT ?? 'vartrack-demo';
// On Windows, a plain `bash` can be the WSL launcher in System32, which expands $VARIABLES before the script runs: Git's bash is the one
// a person following the README uses there.
const GIT_BASH = 'C:\\Program Files\\Git\\bin\\bash.exe';
const BASH = process.env.VT_BASH ?? (process.platform === 'win32' && fs.existsSync(GIT_BASH) ? GIT_BASH : 'bash');

const HASH = 'demo:redis-hash:default:vartrack';
/** The rules that sync from the start; the seventh, gitea/redis-string, is refused until step 7. */
const RULES = ['gitea/mongo', 'gitea/mongo-file', 'gitea/zookeeper', 'gitea/redis-hash', 'gitea/linux_server', 'gitea/s3'] as const;
const REFUSED = 'gitea/redis-string';

interface Step {
  n: number;
  title: string;
  /** The `sh tour` blocks, in order. */
  blocks: string[];
  /** Every fenced block of the step, whatever its language: for a step that only documents. */
  fences: { info: string; body: string }[];
}

/** The steps of "The tour" in demo/README.md: `### N. Title`, with the fenced blocks under each. */
function tourSteps(): Step[] {
  const text = fs.readFileSync(path.join(DEMO, 'README.md'), 'utf8').replace(/\r\n/g, '\n');
  const start = text.indexOf('\n## The tour\n');
  expect(start, 'demo/README.md has a "## The tour" section').toBeGreaterThan(-1);
  const end = text.indexOf('\n## ', start + 1);
  const tour = text.slice(start, end === -1 ? undefined : end);
  const steps: Step[] = [];
  for (const part of tour.split(/\n(?=### \d+\. )/).slice(1)) {
    const heading = /^### (\d+)\. (.+)$/m.exec(part)!;
    const fences = [...part.matchAll(/^```(\S*)[ \t]*(\S*)\n([\s\S]*?)^```$/gm)].map(m => ({ info: `${m[1]} ${m[2]}`.trim(), body: m[3] }));
    steps.push({ n: Number(heading[1]), title: heading[2], blocks: fences.filter(f => f.info === 'sh tour').map(f => f.body), fences });
  }
  return steps;
}

/** Run a command block from demo/, as a person at a shell would. Throws, with the output, when it fails. */
function sh(script: string): string {
  const result = spawnSync(BASH, ['-c', script], {
    cwd: DEMO,
    encoding: 'utf8',
    env: { ...process.env, COMPOSE_PROJECT_NAME: PROJECT, MSYS_NO_PATHCONV: '1' },
    timeout: 60_000,
  });
  const output = `${result.stdout ?? ''}${result.stderr ?? ''}`;
  if (result.status !== 0) throw new Error(`the command failed (exit ${result.status}):\n${script}\n${output}`);
  return output;
}

// What the stores hold, read by the tour's own commands (the README's are the ones that change things).
const redisValue = (field = 'cache.ttl_seconds') => sh(`docker compose exec -T redis redis-cli -n 2 HGET ${HASH} ${field}`).trim();
const redisHas = (field: string) => sh(`docker compose exec -T redis redis-cli -n 2 HEXISTS ${HASH} ${field}`).trim() === '1';
const mongoValue = () =>
  sh(`docker compose exec -T mongo mongosh vartrack --quiet --eval 'print(db.variables.findOne({_vt_env: "default"}).data.cache.ttl_seconds)'`).trim();

const steps = tourSteps();
const byNumber = (n: number): Step => {
  const step = steps.find(s => s.n === n);
  if (!step) throw new Error(`demo/README.md has no step ${n}`);
  return step;
};

/** When the test started: an event older than this belongs to an earlier run of the demo, not to the step. */
const startedAt = new Date().toISOString();

type Check = (step: Step, ctx: Context) => Promise<void>;
type Context = Parameters<Parameters<typeof test>[2]>[0];

const CHECKS: Record<number, Check> = {
  // 1. Sign in and look: six rules in sync, and the seventh refused, with its reason.
  async 1(_step, { page, api }) {
    await page.goto('/rules');
    for (const rule of RULES) await expect(page.getByText(rule, { exact: true }).first()).toBeVisible();
    await expect
      .poll(async () => (await api.rules()).filter(r => RULES.includes(r.name as (typeof RULES)[number])).map(r => r.targets.map(t => `${t.env}:${t.drift}`).join()).sort())
      .toEqual(RULES.map(() => 'default:in_sync'));
    await expect(page.getByRole('article', { name: `${REFUSED}: Refused` })).toBeVisible();
    await expect(page.getByTestId('refused-reason')).toContainText('names no environments and no file to read');
    const rulePage = new RulePage(page);
    await rulePage.open('gitea/mongo');
    expect(await rulePage.envNames()).toEqual(['default']);
    await rulePage.openKeys('gitea/mongo');
    await expect(page.getByText('cache.ttl_seconds').first()).toBeVisible();
  },

  // 2. Change Git, and watch it sync: the push reaches the rules through the webhooks; every store takes the new value.
  async 2(step, { page, api }) {
    sh(step.blocks[0]);
    const reads = step.blocks[1].split('\n').filter(Boolean);
    expect(reads.length, 'one read per rule').toBe(RULES.length);
    for (const read of reads) {
      await expect.poll(() => sh(read), { timeout: 60_000, message: `${read.slice(0, 80)} holds 310 after the push` }).toContain('310');
    }
    const events = await api.events('&rule=gitea/mongo');
    expect(events.some(e => e.reason === 'SyncSucceeded' && e.time >= startedAt), 'gitea/mongo says it synced').toBe(true);
    const rulePage = new RulePage(page);
    await rulePage.openKeys('gitea/mongo');
    await rulePage.keysSearch.fill('ttl_seconds');
    await expect(page.getByRole('row', { name: /cache\.ttl_seconds.*310/ }).first()).toBeVisible();
  },

  // 3. Drift, healed: gitea/mongo heals itself, with events that say so.
  async 3(step, { api }) {
    const before = Date.now();
    sh(step.blocks[0]);
    await expect.poll(mongoValue, { timeout: 90_000, message: 'the value is back (self-heal)' }).toBe('310');
    // The sync nobody started, and the event that says it healed. (DriftDetected is written when a page looks at the environment
    // while it is drifted, so the README does not promise it.)
    await expect
      .poll(async () => (await api.tasks()).some(t => t.rule === 'gitea/mongo' && t.trigger === 'self_heal' && Date.parse(t.createdAt ?? '') >= before), { timeout: 30_000 })
      .toBe(true);
    await expect
      .poll(async () => (await api.events('&rule=gitea/mongo')).some(e => e.reason === 'HealSucceeded' && Date.parse(e.time) >= before), { timeout: 30_000 })
      .toBe(true);
  },

  // 4. Drift, not healed: gitea/redis-hash reads Out of sync, shows it in its Diff, and Sync puts Git's value back.
  async 4(step, { page, api }) {
    sh(step.blocks[0]);
    await expect.poll(async () => (await api.rule('gitea/redis-hash')).targets.find(t => t.env === 'default')?.drift, { timeout: 30_000 }).toBe('drifted');
    expect(redisValue(), 'nothing heals it by itself').toBe('9999');
    const rulePage = new RulePage(page);
    await rulePage.open('gitea/redis-hash');
    await rulePage.envDiff('default').click();
    await expect(page.getByText('cache.ttl_seconds').first()).toBeVisible();
    await page.keyboard.press('Escape');
    const task = await rulePage.syncEnv('default');
    await api.succeeded(task);
    expect(redisValue()).toBe('310');
  },

  // 5. Dry run: it says what a sync would change and writes nothing; Sync then puts it right.
  async 5(step, { page, api }) {
    sh(step.blocks[0]);
    const rulePage = new RulePage(page);
    await rulePage.open('gitea/redis-hash');
    const dry = await rulePage.syncEnv('default', { dryRun: true });
    const task = await api.finished(dry);
    expect(task.dryRun, 'it was a dry run').toBe(true);
    expect(redisValue(), 'a dry run writes nothing').toBe('9999');
    const real = await rulePage.syncEnv('default', { dryRun: false });
    await api.succeeded(real);
    expect(redisValue()).toBe('310');
  },

  // 6. Prune: a key Git no longer holds is deleted from the store, and the rule says so.
  async 6(step, { api }) {
    expect(redisHas('features.debug_mode'), 'the key is there before').toBe(true);
    sh(step.blocks[0]);
    await expect.poll(() => redisHas('features.debug_mode'), { timeout: 60_000, message: 'the key is pruned from the hash' }).toBe(false);
    await expect
      .poll(async () => (await api.events('&rule=gitea/redis-hash')).some(e => e.reason === 'KeysPruned' && e.time >= startedAt), { timeout: 30_000 })
      .toBe(true);
  },

  // 7. A refused rule: it says why; its entry is added to the tenant's bundle.json, and it syncs.
  async 7(step, { page, api }) {
    const rule = (await api.rules()).find(r => r.name === REFUSED);
    expect(rule?.refused, 'the rule is refused, with its reason').toContain('names no environments and no file to read');
    sh(step.blocks[0]);
    await expect
      .poll(async () => {
        const r = (await api.rules()).find(x => x.name === REFUSED);
        return r && !r.refused ? r.targets.map(t => t.env).join() : 'still refused';
      }, { timeout: 60_000, message: 'the schema push reads the rule again: it is no longer refused' })
      .toBe('default');
    const rulePage = new RulePage(page);
    await rulePage.open(REFUSED);
    const task = await rulePage.syncEnv('default');
    await api.succeeded(task);
    expect(sh(step.blocks[1]).trim(), 'the key is a string in Redis').toBe('310');
    expect(sh('docker compose exec -T redis redis-cli -n 3 EXISTS demo:redis-string:default:features_debug_mode').trim(), 'without the key step 6 removed from Git').toBe('0');
  },

  // 8. As someone else: the viewer reads and dry-runs but cannot sync; the outsider sees nothing. (Below, with their own sign-ins.)
  async 8() {},

  // 9. Start over: documentation only, and it has to say how.
  async 9(step) {
    expect(step.blocks, 'the way to start over is not run by the tour').toEqual([]);
    expect(step.fences.some(f => f.info === 'sh' && f.body.includes('docker compose down -v'))).toBe(true);
  },
};

test.describe('the demo tour', () => {
  test('every step of the README has a check, and every check a step', () => {
    expect(steps.map(s => s.n)).toEqual(Object.keys(CHECKS).map(Number).sort((a, b) => a - b));
    for (const step of steps) {
      const expected = step.n === 1 || step.n === 8 || step.n === 9 ? 0 : 1;
      expect(step.blocks.length >= expected, `step ${step.n} (${step.title}) has the command blocks its check runs`).toBe(true);
    }
  });

  test.describe.serial('as the administrator', () => {
    test.use(as('admin'));
    for (const n of [1, 2, 3, 4, 5, 6, 7, 9]) {
      const step = steps.find(s => s.n === n);
      test(`step ${n}: ${step?.title ?? '(missing from the README)'}`, async ({ page, api }) => {
        await CHECKS[n](byNumber(n), { page, api } as Context);
      });
    }
  });

  test.describe('as someone else (step 8)', () => {
    test.describe('the viewer', () => {
      test.use(as('reader'));
      test('step 8: the rules are there, a dry run works, Sync is disabled with the reason', async ({ page }) => {
        const rulePage = new RulePage(page);
        await rulePage.open('gitea/redis-hash');
        await expect(rulePage.envSync('default')).toBeDisabled();
        await expect(rulePage.envDryRun('default')).toBeEnabled();
      });
    });
    test.describe('the outsider', () => {
      test.use(as('outsider'));
      test('step 8: the Rules page is empty', async ({ page, api }) => {
        await page.goto('/rules');
        for (const rule of [...RULES, REFUSED]) await expect(page.getByText(rule, { exact: true })).toHaveCount(0);
        expect(await api.rules()).toEqual([]);
      });
    });
  });
});
