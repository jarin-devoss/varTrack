import { expect, type APIRequestContext } from '@playwright/test';
import { mintGrant } from './grant';

const ORCH = process.env.VT_ORCHESTRATOR_URL ?? 'http://localhost:8000';

/** A target of a rule as the UI API returns it (see ui/src/api/types.ts). */
export interface ApiTarget {
  env: string;
  drift: string;
  fedBy?: string;
  notFed?: string;
  keyCount: number;
  lastSync?: { taskId: string; state: string; trigger?: string };
}

export interface ApiKey {
  key: string;
  gitValue?: string;
  liveValue?: string;
  secret: boolean;
  protected: boolean;
  state: string;
  sourceFile?: string;
}

export interface ApiRule {
  name: string;
  tenant?: string;
  platform: string;
  datasource: string;
  canSync?: boolean;
  selfHeal?: boolean;
  targets: ApiTarget[];
}

export interface ApiFile {
  file: string;
  env: string;
  status: string;
  validation?: string;
  schema?: string;
  schemaSource?: string;
  written: number;
  error?: string;
}

export interface ApiEvent {
  id: string;
  time: string;
  type: 'Normal' | 'Warning';
  reason: string;
  message: string;
  rule: string;
  env?: string;
  taskId?: string;
  tenant?: string;
}

export interface ApiTask {
  taskId: string;
  state: string;
  trigger?: string;
  dryRun?: boolean;
  message?: string;
  rule?: string;
  env?: string;
  tenant?: string;
  submittedBy?: string;
  createdAt?: string;
  finishedAt?: string;
  revision?: string;
  files?: ApiFile[];
}

/**
 * The orchestrator's UI API as the signed-in person (the browser context's own cookies), for checking what the UI
 * shows against what the API says, and for waiting on a task. `stack` calls the orchestrator directly with a signed
 * grant, for the few things only the stack's own services may do.
 */
export class UiApi {
  constructor(private readonly request: APIRequestContext) {}

  private async json<T>(path: string, timeout?: number): Promise<T> {
    const response = await this.request.get(`/api/orchestrator${path}`, { timeout });
    expect(response.status(), `GET ${path}`).toBe(200);
    return (await response.json()) as T;
  }

  session = () => this.json<{ authenticated: boolean; user?: { login: string }; admin?: boolean }>('/v1/auth/session');

  async rules(): Promise<ApiRule[]> {
    const body = await this.json<ApiRule[] | { rules: ApiRule[] }>('/v1/ui/rules');
    return Array.isArray(body) ? body : body.rules;
  }

  rule = (name: string) => this.json<ApiRule>(`/v1/ui/rules/${name}`);
  /** The rule as the Refresh button reads it: the sinks are read now, not answered from the last read. */
  fresh = (name: string) => this.json<ApiRule>(`/v1/ui/rules/${name}?fresh=true`, 40_000);
  /** Newest first; the Tasks page asks for 300. */
  async tasks(limit = 500): Promise<ApiTask[]> {
    return (await this.json<{ tasks: ApiTask[] }>(`/v1/ui/tasks?limit=${limit}`)).tasks;
  }
  task = (id: string) => this.json<ApiTask>(`/v1/ui/tasks/${id}`);

  /** A dry run through the gateway's CLI API, as `vt sync --dry-run` makes one: needs a Git token (Gitea.tokenFor). Returns the task. */
  async cliDryRun(token: string, datasource: string, env: string, content: string, filePath = 'app-settings.yaml'): Promise<string> {
    const response = await this.request.post('/api/gateway/v1/cli/sync', {
      headers: { Authorization: `Bearer ${token}` },
      data: { datasource, platform: 'gitea', env, file_path: filePath, content, dry_run: true, label: 'ui-e2e', tenant_id: 'demo' },
    });
    expect(response.status(), `POST cli/sync: ${await response.text()}`).toBe(202);
    return ((await response.json()) as { task_id: string }).task_id;
  }

  /** Start a sync the way the Sync panel does, without the panel: for specs that need a task, not the panel. */
  async sync(rule: string, envs: string[], options: { dryRun?: boolean; prune?: boolean } = {}): Promise<string[]> {
    const response = await this.request.post(`/api/orchestrator/v1/ui/rules/${rule}/sync`, { data: { envs, dryRun: options.dryRun ?? false, prune: options.prune ?? false } });
    expect(response.status(), `POST sync ${rule}: ${await response.text()}`).toBe(200);
    return ((await response.json()) as { taskIds: string[] }).taskIds;
  }
  /** Forget an environment the rule no longer produces (an administrator may; one still fed is refused). */
  async forget(rule: string, env: string, tenant = 'demo'): Promise<void> {
    const response = await this.request.delete(`/api/orchestrator/v1/ui/rules/${rule}/targets/${encodeURIComponent(env)}?tenant=${tenant}`);
    expect(response.status(), `forget ${env} of ${rule}: ${await response.text()}`).toBe(204);
  }
  async events(query = ''): Promise<ApiEvent[]> {
    return (await this.json<{ events: ApiEvent[] }>(`/v1/ui/events?limit=400${query}`)).events;
  }
  /** The keys of one environment: what Git says (`gitValue`) next to what the sink holds (`liveValue`). */
  keys = (rule: string, env: string) => this.json<{ keys: ApiKey[] }>(`/v1/ui/rules/${rule}/targets/${env}/keys`);

  /**
   * Wait for the task to finish, polling the API (not the clock). A task just queued is not in the UI's list until a
   * worker has picked it up, so "not found yet" is a reason to keep waiting, not to fail.
   */
  /** The task, once it has finished, which it must have done by succeeding: a task that failed fails the spec at once, with its message. */
  async succeeded(taskId: string, timeout = 45_000): Promise<ApiTask> {
    const task = await this.finished(taskId, timeout);
    expect(task.state, `task ${taskId}: ${task.message}`).toBe('SUCCEEDED');
    return task;
  }

  async finished(taskId: string, timeout = 45_000): Promise<ApiTask> {
    let last: ApiTask | undefined;
    await expect
      .poll(
        async () => {
          const response = await this.request.get(`/api/orchestrator/v1/ui/tasks/${taskId}`);
          if (response.status() === 404) return 'not recorded yet';
          expect(response.status(), `GET task ${taskId}`).toBe(200);
          last = (await response.json()) as ApiTask;
          return last.state;
        },
        { timeout, message: `task ${taskId} finishes` },
      )
      .toMatch(/^(SUCCEEDED|FAILED)$/);
    return last as ApiTask;
  }
}

/** Where the watcher's admin port is reached (the demo publishes it on :9091). */
export const WATCHER = process.env.VT_WATCHER_URL ?? 'http://localhost:9091';

/** What the watcher found when asked to look at a rule now. */
export interface WatcherCheck {
  watcher: string;
  tenant: string;
  state: string;
  detail: string;
  healed: boolean;
  error?: string;
}

/** Calls only the stack's own services make, signed with the demo's internal secret. */
export class Stack {
  constructor(private readonly request: APIRequestContext) {}

  /**
   * Make the watcher look at a rule now and heal it if it drifted, instead of at its next poll: the way a spec asks
   * rather than waits. Returns what it found.
   */
  async checkNow(platform: string, datasource: string, tenant = 'demo'): Promise<WatcherCheck[]> {
    const response = await this.request.post(`${WATCHER}/admin/check`, {
      params: { platform, datasource, tenant },
      headers: { Authorization: `Bearer ${mintGrant({ audience: 'admin' })}` },
      timeout: 100_000,
    });
    expect(response.status(), `watcher check of ${platform}/${datasource}: ${await response.text()}`).toBe(200);
    return ((await response.json()) as { results: WatcherCheck[] }).results;
  }

  /** Have the orchestrator read the tenant's schema repository afresh, so a bundle.json just pushed is the one used. */
  async warm(tenant = 'demo'): Promise<{ schemaCommit: string; rulesLoaded: number }> {
    const response = await this.request.post(`${ORCH}/v1/tenants/${tenant}/schemas/warm`, {
      headers: { Authorization: `Bearer ${mintGrant()}`, 'Content-Type': 'application/json' },
    });
    expect(response.status(), 'schema warm-up').toBe(200);
    const body = (await response.json()) as { schema_commit: string; rules_loaded: number };
    return { schemaCommit: body.schema_commit, rulesLoaded: body.rules_loaded };
  }

  /**
   * A push, as the gateway forwards one: the webhook a Gitea push to *branch* of the config repository sends for
   * *datasource*, touching *files*. Returns the task it queued. (The clone URL is the one inside the stack's network, where the
   * orchestrator reads from.)
   */
  async push(datasource: string, branch: string, sha: string, files: string[] = ['app-settings.yaml']): Promise<string> {
    const clone = 'http://gitea:3000/vartrack-demo/demo-configs.git';
    const payload = {
      ref: `refs/heads/${branch}`,
      before: '0'.repeat(40),
      after: sha,
      repository: {
        name: 'demo-configs',
        full_name: 'vartrack-demo/demo-configs',
        owner: { name: 'vartrack-demo', login: 'vartrack-demo' },
        clone_url: clone,
        html_url: 'http://gitea:3000/vartrack-demo/demo-configs',
        default_branch: 'main',
      },
      pusher: { name: 'ui-e2e', email: 'ui-e2e@vartrack.local' },
      commits: [{ id: sha, message: 'ui-e2e push', added: files, removed: [], modified: [] }],
      head_commit: { id: sha, message: 'ui-e2e push', added: files, removed: [], modified: [] },
    };
    const response = await this.request.post(`${ORCH}/v1/webhooks/${datasource}`, {
      headers: {
        Authorization: `Bearer ${mintGrant({ datasource })}`,
        'Content-Type': 'application/json',
        'X-Gitea-Event': 'push',
      },
      data: payload,
    });
    expect(response.status(), `push to ${branch} for ${datasource}`).toBe(200);
    return ((await response.json()) as { task_id: string }).task_id;
  }
}
