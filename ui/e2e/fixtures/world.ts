import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { request } from '@playwright/test';
import { Stack, UiApi } from './api';
import { PEOPLE } from './roles';
import { CONFIG_REPO, Gitea, SCHEMA_REPO } from './gitea';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..', '..');
const read = (...parts: string[]) => fs.readFileSync(path.join(repoRoot, ...parts), 'utf8');

/**
 * What the specs find when they start: the same repositories, branches, schemas and rules whatever an earlier run or
 * the 23 regression suites left behind. It is built through the product, as a person would:
 *
 *   - `develop` exists next to `main`, both holding the demo's config file, and team-a/ and team-b/ hold one each;
 *   - the schema repository holds the CUE schemas and `world/bundle.json`, a rule for each kind of environment;
 *   - the orchestrator is told to read it afresh (the one call only the stack's own services make).
 *
 * The rules (see world/bundle.json) and the environments they produce:
 *
 *   redis-hash      branch_map {main: production, develop: staging}   production (main, protected), staging (develop)
 *   redis-string    branch_as_env                                      main (protected), develop
 *   zookeeper       no environment strategy, self-heal                 default (main, protected)
 *   mongo-document  file_path_map, branch develop                      team-a, team-b
 *   mongo           no strategy, branch develop, self-heal             default (develop)
 *   mongo-primary   no strategy, branch develop, no self-heal          default (develop)
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
      const done = await seedEnvironments(gitea, stack, new UiApi(admin));
      if (done !== FIRST_PUSHES.length) throw new Error(`${done} of ${FIRST_PUSHES.length} first syncs succeeded`);
    } finally {
      await admin.dispose();
    }
  } finally {
    await gitea.dispose();
    await http.dispose();
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
