import { expect, request, type APIRequestContext } from '@playwright/test';

export const GITEA = process.env.VT_GITEA_URL ?? 'http://localhost:3000';
const url = (path: string): string => `${GITEA}/api/v1${path}`;
export const ORG = 'vartrack-demo';
export const CONFIG_REPO = 'demo-configs';
export const SCHEMA_REPO = 'schemas';

/**
 * The demo's Git server, as its admin (demo-admin): what a person does with git, so a spec can push a change and
 * see what the UI makes of it. Every value a spec pushes is unique (`unique()`), so one spec cannot be fooled by
 * another's leftovers.
 */
export class Gitea {
  private constructor(private readonly api: APIRequestContext) {}

  static async connect(): Promise<Gitea> {
    const api = await request.newContext({
      httpCredentials: { username: 'demo-admin', password: 'VarTrack123', send: 'always' },
    });
    return new Gitea(api);
  }

  dispose = () => this.api.dispose();

  /**
   * A personal access token for one of the demo's people, made the way they would in Gitea's settings: the one a person
   * gives `vt login`, and the Playground. A new one per call, so a spec's token is its own.
   */
  static async tokenFor(login: string, password: string): Promise<string> {
    const api = await request.newContext({ httpCredentials: { username: login, password, send: 'always' } });
    try {
      const response = await api.post(url(`/users/${login}/tokens`), {
        data: { name: `ui-e2e-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`, scopes: ['read:user', 'read:repository', 'write:repository'] },
      });
      expect(response.status(), `a token for ${login}: ${await response.text()}`).toBe(201);
      return ((await response.json()) as { sha1: string }).sha1;
    } finally {
      await api.dispose();
    }
  }

  /** A new account that has signed in nowhere (so it has not authorized any application yet), as an administrator makes one. */
  async createUser(login: string, password: string): Promise<void> {
    const response = await this.api.post(url('/admin/users'), {
      data: { username: login, email: `${login}@vartrack.local`, password, must_change_password: false, send_notify: false },
    });
    expect(response.status(), `create ${login}: ${await response.text()}`).toBe(201);
  }

  async deleteUser(login: string): Promise<void> {
    const response = await this.api.delete(url(`/admin/users/${login}?purge=true`));
    expect([204, 404], `delete ${login}`).toContain(response.status());
  }

  /** The commit a branch is at. */
  async head(repo: string, branch = 'main'): Promise<string> {
    const response = await this.api.get(url(`/repos/${ORG}/${repo}/branches/${encodeURIComponent(branch)}`));
    expect(response.status(), `branch ${branch} of ${repo}`).toBe(200);
    return ((await response.json()) as { commit: { id: string } }).commit.id;
  }

  async hasBranch(repo: string, branch: string): Promise<boolean> {
    return (await this.api.get(url(`/repos/${ORG}/${repo}/branches/${encodeURIComponent(branch)}`))).status() === 200;
  }

  /** Create *branch* from *from* unless it is there. */
  async ensureBranch(repo: string, branch: string, from = 'main'): Promise<void> {
    if (await this.hasBranch(repo, branch)) return;
    const response = await this.api.post(url(`/repos/${ORG}/${repo}/branches`), {
      data: { new_branch_name: branch, old_branch_name: from },
    });
    expect([201, 409]).toContain(response.status());
  }

  async deleteBranch(repo: string, branch: string): Promise<void> {
    await this.api.delete(url(`/repos/${ORG}/${repo}/branches/${encodeURIComponent(branch)}`));
  }

  /** The text of a file at a branch, or undefined. */
  async read(repo: string, filePath: string, branch = 'main'): Promise<string | undefined> {
    const response = await this.api.get(url(`/repos/${ORG}/${repo}/contents/${filePath}`), { params: { ref: branch } });
    if (response.status() !== 200) return undefined;
    return Buffer.from(((await response.json()) as { content: string }).content, 'base64').toString('utf8');
  }

  /**
   * Create or update a file on a branch; returns the commit it made, or the branch's head when the file already
   * holds exactly this text. Throws when Gitea refuses (a protected branch, say).
   */
  async put(repo: string, filePath: string, text: string, message: string, branch = 'main'): Promise<string> {
    const endpoint = url(`/repos/${ORG}/${repo}/contents/${filePath}`);
    const existing = await this.api.get(endpoint, { params: { ref: branch } });
    const content = Buffer.from(text, 'utf8').toString('base64');
    let response: Awaited<ReturnType<APIRequestContext['put']>>;
    if (existing.status() === 200) {
      const found = (await existing.json()) as { sha: string; content: string };
      if (found.content.replace(/\n/g, '') === content) return this.head(repo, branch);
      response = await this.api.put(endpoint, { data: { message, content, sha: found.sha, branch } });
    } else {
      response = await this.api.post(endpoint, { data: { message, content, branch } });
    }
    if (!response.ok()) throw new Error(`could not push ${repo}/${filePath} to ${branch}: ${response.status()} ${(await response.text()).slice(0, 160)}`);
    return ((await response.json()) as { commit: { sha: string } }).commit.sha;
  }
}

/** A value no other spec, and no earlier run, has used. */
export const unique = (label: string): string => `${label}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
