import { test as base, expect, request } from '@playwright/test';
import { Stack, UiApi } from './api';
import { Faults } from './faults';
import { Gitea } from './gitea';
import { PEOPLE, type Role } from './roles';
import { Stores } from './stores';
import { Tasks } from './tasks';

type Fixtures = {
  /** The orchestrator's UI API as the person the spec is signed in as. */
  api: UiApi;
  /** The same API as the admin, whoever the spec is signed in as: for comparing what the person sees with everything there is. */
  adminApi: UiApi;
  /** Calls only the stack's own services make (a push webhook, a schema warm-up), signed with the demo's secret. */
  stack: Stack;
  /** The demo's Git server, as its admin. */
  gitea: Gitea;
  /** What the sinks hold, read and changed directly. */
  stores: Stores;
  /** Stop and start a service; whatever is stopped is started again after the spec. */
  faults: Faults;
  /** A task of each kind (works, dry run, refused), made as the admin when a spec asks for one. */
  tasks: Tasks;
};

/** Tasks a spec only reads: made the first time a spec asks, then the same for the rest of the run. */
export interface Made {
  /** A real sync that worked. */
  ok(): Promise<string>;
  /** A dry run. */
  dry(): Promise<string>;
  /** A sync a strict schema refused, and the invalid value it was refused for. */
  failed(): Promise<{ id: string; bad: string }>;
}

type WorkerFixtures = { made: Made };

export const test = base.extend<Fixtures, WorkerFixtures>({
  made: [
    async ({ playwright }, use) => {
      const admin = await playwright.request.newContext({ baseURL: process.env.VT_UI_URL ?? 'http://localhost:8080', storageState: PEOPLE.admin.storageState });
      const gitea = await Gitea.connect();
      const tasks = new Tasks(new UiApi(admin), gitea);
      const once = <T>(make: () => Promise<T>): (() => Promise<T>) => {
        let made: Promise<T> | undefined;
        return () => (made ??= make().catch(error => {
          made = undefined; // a failed attempt is not kept: the next spec tries again
          throw error;
        }));
      };
      await use({ ok: once(() => tasks.ok()), dry: once(() => tasks.dry()), failed: once(() => tasks.failed()) });
      await gitea.dispose();
      await admin.dispose();
    },
    { scope: 'worker' },
  ],

  api: async ({ context }, use) => {
    await use(new UiApi(context.request));
  },
  adminApi: async ({ playwright, baseURL }, use) => {
    const admin = await playwright.request.newContext({ baseURL, storageState: PEOPLE.admin.storageState });
    await use(new UiApi(admin));
    await admin.dispose();
  },
  stack: async ({}, use) => {
    const http = await request.newContext();
    await use(new Stack(http));
    await http.dispose();
  },
  gitea: async ({}, use) => {
    const gitea = await Gitea.connect();
    await use(gitea);
    await gitea.dispose();
  },
  stores: async ({}, use) => {
    await use(new Stores());
  },
  tasks: async ({ adminApi, gitea }, use) => {
    await use(new Tasks(adminApi, gitea));
  },
  faults: async ({}, use) => {
    const faults = new Faults();
    await use(faults);
    await faults.restore();
  },
});

export { expect };
export { unique } from './gitea';
export { WORLD } from './world';
export { PEOPLE } from './roles';
export type { Role } from './roles';

/** `test.use(as('writer'))`: the spec starts signed in as that person (the state global-setup saved). */
export const as = (role: Role) => ({ storageState: PEOPLE[role].storageState });
