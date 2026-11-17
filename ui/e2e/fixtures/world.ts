import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { request } from '@playwright/test';
import { Stack, UiApi, type ApiRule } from './api';
import { PEOPLE } from './roles';
import { CONFIG_REPO, Gitea, SCHEMA_REPO } from './gitea';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..', '..');
const read = (...parts: string[]) => fs.readFileSync(path.join(repoRoot, ...parts), 'utf8');

/**
 * What the specs find when they start: the same repositories, branches, schemas and rules whatever an earlier run or
 * the 25 regression suites left behind. It is built through the product, as a person would:
 *
 *   - `develop` exists next to `main`, both holding the demo's config file, and team-a/ and team-b/ hold one each;
 *   - the schema repository holds the CUE schemas and `world/bundle.json`, a rule for each kind of environment;
 *   - the orchestrator is told to read it afresh (the one call only the stack's own services make).
 *
 * The rules (see world/bundle.json) and the environments they produce:
 *
 *   redis-hash      branch_map {main: production, develop: staging}   production (main, protected), staging (develop)
 *   redis-string    branch_as_env                                      main (protected), develop
 *   zookeeper       branch_map {main: production}, self-heal           production (main, protected)
 *   mongo-document  file_path_map, branch develop                      team-a, team-b
 *   mongo           branch_map {develop: staging}, self-heal           staging (develop)
 *   mongo-primary   branch_map {develop: staging}, no self-heal        staging (develop)
 *
 * Five more rules (mongo-file, linux_server, s3, mongo-dr, mongo-kv) feed `production` from main and are never synced here.
 * `WORLD.environments` holds all of it as data: setup forgets any other environment of these rules and then checks the list.
 */
export const WORLD = {
  tenant: 'demo',
  rules: {
    branchMap: 'gitea/redis-hash',
    branchAsEnv: 'gitea/redis-string',
    defaultProtected: 'gitea/zookeeper',
    filePath: 'gitea/mongo-document',
    selfHeal: 'gitea/mongo',
    noSelfHeal: 'gitea/mongo-primary',
  },
  /** Each rule's environments, and only these: what a spec may rely on finding. */
  environments: {
    'gitea/redis-hash': ['production', 'staging'],
    'gitea/redis-string': ['develop', 'main'],
    'gitea/zookeeper': ['production'],
    'gitea/mongo-document': ['team-a', 'team-b'],
    'gitea/mongo': ['staging'],
    'gitea/mongo-primary': ['staging'],
    'gitea/mongo-file': ['production'],
    'gitea/linux_server': ['production'],
    'gitea/s3': ['production'],
    'gitea/mongo-dr': ['production'],
    'gitea/mongo-kv': ['production'],
  } as Record<string, readonly string[]>,
  protectedBranch: 'main',
  openBranch: 'develop',
} as const;

const SETTINGS = 'app-settings.yaml';

export async function buildWorld(baseURL: string): Promise<void> {
  const gitea = await Gitea.connect();
  const http = await request.newContext();
  try {
    const settings = read('e2e', 'configs', SETTINGS);

    // Branches and files. main is protected (only demo-admin pushes), so the admin's pushes to it are the ones that land.
    await gitea.ensureBranch(CONFIG_REPO, WORLD.openBranch);
    for (const branch of ['main', WORLD.openBranch]) {
      await gitea.put(CONFIG_REPO, SETTINGS, settings, 'ui-e2e: the demo config file', branch);
      for (const team of ['team-a', 'team-b']) {
        await gitea.put(CONFIG_REPO, `${team}/${SETTINGS}`, `owner: ${team}\n`, `ui-e2e: ${team}'s file`, branch);
      }
    }

    // Schemas and the rules.
    await gitea.put(SCHEMA_REPO, `${SETTINGS}.cue`, read('e2e', 'schemas', 'cue', `${SETTINGS}.cue`), 'ui-e2e: schema');
    for (const team of ['team-a', 'team-b']) {
      await gitea.put(SCHEMA_REPO, `teams/${team}.cue`, read('e2e', 'schemas', 'cue', 'teams', `${team}.cue`), `ui-e2e: ${team} schema`);
    }
    await gitea.put(SCHEMA_REPO, 'bundle.json', read('ui', 'e2e', 'world', 'bundle.json'), 'ui-e2e: the rules');

    const stack = new Stack(http);
    const warmed = await stack.warm(WORLD.tenant);
    if (warmed.rulesLoaded < 6) throw new Error(`the orchestrator loaded ${warmed.rulesLoaded} rules; the world has 6`);

    // Every environment synced once, with the admin's own session reading the tasks.
    const admin = await request.newContext({ baseURL, storageState: PEOPLE.admin.storageState });
    try {
      const api = new UiApi(admin);
      const done = await seedEnvironments(gitea, stack, api);
      if (done !== FIRST_PUSHES.length) throw new Error(`${done} of ${FIRST_PUSHES.length} first syncs succeeded`);
      // An environment an earlier run left behind (one no rule produces now) would be in every spec's way: forget it, and say which.
      const forgotten = await forgetLeftovers(api);
      // An environment a branch_as_env rule still counts as fed (any name is a branch there), such as the `default` the regression
      // suites write, needs the rule to stop saying so for a moment: see forgetStrays.
      forgotten.push(...(await forgetStrays(gitea, stack, api)));
      if (forgotten.length) console.log(`world: forgot ${forgotten.length} leftover environment(s): ${forgotten.join(', ')}`);
      // The world is what it says, or no spec starts.
      const problems = worldProblems(await api.rules());
      if (problems.length) throw new Error(`world: ${problems.join('; ')}`);
    } finally {
      await admin.dispose();
    }
  } finally {
    await gitea.dispose();
    await http.dispose();
  }
}

/** Forget every environment that no rule produces any more, as an administrator does. Returns `rule env` for each. */
export async function forgetLeftovers(api: UiApi): Promise<string[]> {
  const forgotten: string[] = [];
  for (const rule of await api.rules()) {
    for (const target of rule.targets.filter(t => t.notFed)) {
      await api.forget(rule.name, target.env);
      forgotten.push(`${rule.name} ${target.env}`);
    }
  }
  return forgotten;
}

/**
 * Where the rules' environments are not the ones `WORLD.environments` names: one line per rule, `<rule> has <what it has>; the
 * world has <what it names>`. Empty when the world is what it says. A rule of the world that is missing is reported too.
 */
export function worldProblems(rules: Array<Pick<ApiRule, 'name' | 'targets'>>): string[] {
  const problems: string[] = [];
  for (const [name, wanted] of Object.entries(WORLD.environments)) {
    const rule = rules.find(r => r.name === name);
    if (!rule) {
      problems.push(`${name} is not there; the world has it with ${wanted.join(', ')}`);
      continue;
    }
    const have = rule.targets.map(t => t.env).sort();
    const want = [...wanted].sort();
    if (have.join('\n') !== want.join('\n')) problems.push(`${name} has ${have.join(', ') || 'no environment'}; the world has ${want.join(', ')}`);
  }
  return problems;
}

/**
 * Forget the environments of the world's rules that the world does not name and that a branch still feeds. Under `branch_as_env`
 * any name is its own branch, so such an environment (the regression suites write `default` into redis-string) reads as fed and
 * the engine will not forget it. As an administrator would: the rule is changed to name only its own branches, the stray is no longer
 * fed and is forgotten, and the rule is put back as the world has it. Does nothing, and writes nothing, when there is none.
 * Returns `rule env` for each.
 */
export async function forgetStrays(gitea: Gitea, stack: Stack, api: UiApi): Promise<string[]> {
  const strays: Array<{ rule: string; env: string }> = [];
  for (const rule of await api.rules()) {
    const named = WORLD.environments[rule.name];
    if (!named) continue;
    for (const target of rule.targets.filter(t => !named.includes(t.env))) strays.push({ rule: rule.name, env: target.env });
  }
  const names = [...new Set(strays.map(s => s.rule))];
  const bundle = JSON.parse(read('ui', 'e2e', 'world', 'bundle.json')) as Array<Record<string, unknown>>;
  const changed = names.filter(name => {
    const entry = bundle.find(r => `${r.platform}/${r.datasource}` === name);
    // only a branch_as_env rule can be fed a stray name; for any other the check at the end of setup says what is wrong
    if (!entry?.branch_as_env) return false;
    Object.assign(entry, { branch_as_env: false, branch_map: Object.fromEntries(WORLD.environments[name].map(env => [env, env])) });
    return true;
  });
  if (!changed.length) return [];

  const forgotten: string[] = [];
  try {
    await gitea.put(SCHEMA_REPO, 'bundle.json', JSON.stringify(bundle, null, 2), 'ui-e2e: the rules, naming only their own branches');
    await stack.warm(WORLD.tenant);
    for (const { rule, env } of strays.filter(s => changed.includes(s.rule))) {
      await api.forget(rule, env);
      forgotten.push(`${rule} ${env}`);
    }
  } finally {
    await gitea.put(SCHEMA_REPO, 'bundle.json', read('ui', 'e2e', 'world', 'bundle.json'), 'ui-e2e: the rules, as they were');
    await stack.warm(WORLD.tenant);
  }
  return forgotten;
}

/**
 * Leave an environment behind on purpose, for a spec about leftovers: the rule feeds `env` from `branch` for the moment it takes
 * to write it, then is put back as the world has it, so nothing feeds `env` any more. `forgetLeftover` takes it away again.
 */
export async function leaveBehind(baseURL: string, datasource: string, env: string, branch: string): Promise<void> {
  const gitea = await Gitea.connect();
  const http = await request.newContext();
  const admin = await request.newContext({ baseURL, storageState: PEOPLE.admin.storageState });
  try {
    const stack = new Stack(http);
    const rules = JSON.parse(read('ui', 'e2e', 'world', 'bundle.json')) as Array<Record<string, unknown>>;
    Object.assign(rules.find(r => r.datasource === datasource)!, { branch_map: { [branch]: env } });
    await gitea.put(SCHEMA_REPO, 'bundle.json', JSON.stringify(rules, null, 2), `ui-e2e: ${branch} feeds ${env} for a moment`);
    await stack.warm(WORLD.tenant);
    const api = new UiApi(admin);
    await api.succeeded((await api.sync(`gitea/${datasource}`, [env]))[0], 90_000);
    await gitea.put(SCHEMA_REPO, 'bundle.json', read('ui', 'e2e', 'world', 'bundle.json'), 'ui-e2e: the rules, as they were');
    await stack.warm(WORLD.tenant);
  } finally {
    await admin.dispose();
    await http.dispose();
    await gitea.dispose();
  }
}

export async function forgetLeftover(baseURL: string, rule: string, env: string): Promise<void> {
  const admin = await request.newContext({ baseURL, storageState: PEOPLE.admin.storageState });
  try {
    await new UiApi(admin).forget(rule, env);
  } finally {
    await admin.dispose();
  }
}

/** The pushes that give each rule its environments: [datasource, branch]. A branch that feeds nothing is not listed. */
const FIRST_PUSHES: Array<[string, string]> = [
  ['redis-hash', 'main'], ['redis-hash', 'develop'],
  ['redis-string', 'main'], ['redis-string', 'develop'],
  ['zookeeper', 'main'],
  ['mongo-document', 'develop'],
  ['mongo', 'develop'],
  ['mongo-primary', 'develop'],
];

/**
 * Push to every branch that feeds an environment and wait for each task, so every environment of the world has been
 * synced once and the UI has something to show. Returns how many tasks finished SUCCEEDED.
 */
export async function seedEnvironments(gitea: Gitea, stack: Stack, api: UiApi): Promise<number> {
  const tasks: string[] = [];
  for (const [datasource, branch] of FIRST_PUSHES) {
    tasks.push(await stack.push(datasource, branch, await gitea.head(CONFIG_REPO, branch)));
  }
  let succeeded = 0;
  for (const id of tasks) if ((await api.finished(id, 90_000)).state === 'SUCCEEDED') succeeded += 1;
  return succeeded;
}
