import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

/**
 * The four people the specs sign in as, all Gitea accounts the demo creates (e2e/init/ui_sign_in.py):
 *
 *   admin     demo-admin     a varTrack admin (config.cue's ui.auth.admins) and the owner of the demo repositories
 *   writer    demo-writer    can push to the config repository, except to its protected branch (main)
 *   reader    demo-viewer    can read it
 *   outsider  demo-outsider  can read none of it (the config repository is private)
 */
export type Role = 'admin' | 'writer' | 'reader' | 'outsider';

export interface Person {
  role: Role;
  login: string;
  password: string;
  /** Sees every rule, and may sync whatever a branch feeds. */
  admin: boolean;
  /** The browser state saved by global-setup after signing in through Gitea's own page. */
  storageState: string;
}

const person = (role: Role, login: string, admin = false): Person => ({
  role,
  login,
  password: 'VarTrack123',
  admin,
  storageState: path.join(here, '..', '.auth', `${role}.json`),
});

export const PEOPLE: Record<Role, Person> = {
  admin: person('admin', 'demo-admin', true),
  writer: person('writer', 'demo-writer'),
  reader: person('reader', 'demo-viewer'),
  outsider: person('outsider', 'demo-outsider'),
};

export const ROLES = Object.keys(PEOPLE) as Role[];
