import type { VarTrackApi } from './index';
import { ApiError, requestJson } from './http';
import type {
  BundleView,
  FileResult,
  FileStatus,
  KeyEntry,
  RuleView,
  ServiceHealth,
  ServiceName,
  Task,
  TaskState,
  VtEvent,
} from './types';

export interface LiveConfig {
  gatewayUrl: string;
  orchestratorUrl: string;
  watcherUrl: string;
  token: string;
  tenant: string;
}

const TASK_STATES: TaskState[] = ['PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'RETRYING'];
const FILE_STATUSES: FileStatus[] = ['ok', 'empty', 'validation_failed', 'error', 'skipped', 'aborted'];

/** Celery state + optional result dict → task state. */
export function mapTaskState(celeryState: string, resultState?: unknown): TaskState {
  if (typeof resultState === 'string' && TASK_STATES.includes(resultState as TaskState)) {
    return resultState as TaskState;
  }
  switch (celeryState) {
    case 'STARTED':
    case 'PROGRESS':
      return 'RUNNING';
    case 'RETRY':
      return 'RETRYING';
    case 'SUCCESS':
      return 'SUCCEEDED';
    case 'FAILURE':
      return 'FAILED';
    default:
      return 'PENDING';
  }
}

type Raw = Record<string, unknown>;
const str = (v: unknown) => (typeof v === 'string' ? v : undefined);
const num = (v: unknown) => (typeof v === 'number' ? v : 0);

export function mapFileResult(raw: Raw): FileResult {
  const status = str(raw.status);
  return {
    file: str(raw.file) ?? '',
    env: str(raw.env) ?? 'unknown',
    // Dry-run file reports carry no status; they were parsed successfully.
    status: FILE_STATUSES.includes(status as FileStatus) ? (status as FileStatus) : 'ok',
    syncMode: str(raw.sync_mode) ?? '',
    written: num(raw.written),
    pruned: num(raw.pruned),
    rootKey: str(raw.root_key),
    error: str(raw.error),
    keys: typeof raw.keys === 'number' ? raw.keys : undefined,
    validation: str(raw.validation),
    sample: raw.sample && typeof raw.sample === 'object' ? (raw.sample as Record<string, string>) : undefined,
  };
}

export function createLiveApi(cfg: LiveConfig): VarTrackApi {
  const gw = (path: string) => `${cfg.gatewayUrl.replace(/\/$/, '')}${path}`;
  const orch = (path: string) => `${cfg.orchestratorUrl.replace(/\/$/, '')}${path}`;
  const auth = { token: cfg.token || undefined, tenant: cfg.tenant || undefined };
  const rulePath = (name: string) => name.split('/').map(encodeURIComponent).join('/');

  async function probe(name: ServiceName, url: string): Promise<ServiceHealth> {
    const checkedAt = new Date().toISOString();
    try {
      const { status, body } = await requestJson<unknown>(url, { timeoutMs: 5000, acceptStatus: [503] });
      const checks: Record<string, string> = {};
      if (body && typeof body === 'object') {
        for (const [k, v] of Object.entries(body as Raw)) {
          if (k !== 'status' && (typeof v === 'string' || typeof v === 'number')) checks[k] = String(v);
        }
      }
      return { name, status: status === 200 ? 'up' : 'degraded', checks, checkedAt };
    } catch (err) {
      return { name, status: 'down', checks: {}, checkedAt, error: (err as Error).message };
    }
  }

  return {
    async listRules() {
      const { body } = await requestJson<{ rules: RuleView[] }>(orch('/v1/ui/rules'), auth);
      return body.rules;
    },

    async getRule(name) {
      const { body } = await requestJson<RuleView>(orch(`/v1/ui/rules/${rulePath(name)}`), auth);
      return body;
    },

    async listKeys(rule, env) {
      const url = orch(`/v1/ui/rules/${rulePath(rule)}/targets/${encodeURIComponent(env)}/keys`);
      const { body } = await requestJson<{ keys: KeyEntry[] }>(url, auth);
      return body.keys;
    },

    async listEvents(query = {}) {
      const params = new URLSearchParams();
      if (query.rule) params.set('rule', query.rule);
      params.set('limit', String(query.limit ?? 200));
      const { body } = await requestJson<{ events: VtEvent[] }>(orch(`/v1/ui/events?${params}`), auth);
      return body.events;
    },

    async listTasks(query = {}) {
      const params = new URLSearchParams({ limit: String(query.limit ?? 100) });
      if (query.rule) params.set('rule', query.rule);
      const { body } = await requestJson<{ tasks: Task[] }>(orch(`/v1/ui/tasks?${params}`), auth);
      return body.tasks;
    },

    async getTask(taskId) {
      // The orchestrator records every task it runs; older or foreign tasks
      // fall back to the raw Celery status.
      const recorded = await requestJson<Task>(orch(`/v1/ui/tasks/${encodeURIComponent(taskId)}`), { ...auth, acceptStatus: [404] });
      if (recorded.status === 200) return recorded.body;

      // A failed Celery task is reported as HTTP 500 with the failure as detail.
      const { status, body } = await requestJson<Raw>(orch(`/v1/tasks/${encodeURIComponent(taskId)}`), {
        acceptStatus: [500],
      });
      if (status === 500) {
        return {
          taskId,
          state: 'FAILED',
          message: str(body?.detail) ?? 'task failed',
          trigger: 'webhook',
          datasource: '',
          dryRun: false,
          createdAt: '',
          files: [],
          partial: true,
        } satisfies Task;
      }
      const result = (body.result ?? {}) as Raw;
      const files = Array.isArray(result.files) ? (result.files as Raw[]).map(mapFileResult) : [];
      return {
        taskId,
        state: mapTaskState(str(body.state) ?? '', result.state),
        message: str(result.message) ?? '',
        trigger: 'webhook',
        datasource: '',
        ref: str(result.branch),
        revision: str(result.commit_sha),
        dryRun: Boolean(result.dry_run),
        createdAt: typeof result.received_at === 'number' ? new Date(result.received_at * 1000).toISOString() : '',
        files,
        partial: true,
      } satisfies Task;
    },

    async sync(req) {
      const { body } = await requestJson<{ taskIds: string[] }>(orch(`/v1/ui/rules/${rulePath(req.rule)}/sync`), {
        ...auth,
        method: 'POST',
        body: { envs: req.envs, dryRun: req.dryRun, prune: req.prune },
      });
      return body;
    },

    async validate(req) {
      const { body } = await requestJson<{ status: 'ok' | 'warn' | 'failed'; messages: string[]; key_count: number }>(
        gw('/v1/cli/validate'),
        {
          ...auth,
          method: 'POST',
          body: { file_path: req.filePath, content: req.content, datasource: req.datasource, tenant_id: cfg.tenant },
        },
      );
      return { status: body.status, messages: body.messages ?? [], keyCount: body.key_count ?? 0 };
    },

    async cliSync(req) {
      const { body } = await requestJson<{ task_id: string }>(gw('/v1/cli/sync'), {
        ...auth,
        method: 'POST',
        body: {
          datasource: req.datasource,
          env: req.env,
          file_path: req.filePath,
          content: req.content,
          dry_run: req.dryRun,
          label: req.label,
          tenant_id: cfg.tenant,
        },
      });
      if (!body?.task_id) throw new ApiError('server did not return a task id', 200, gw('/v1/cli/sync'));
      return { taskId: body.task_id };
    },

    async getHealth() {
      return Promise.all([
        probe('gateway', gw('/health/readiness')),
        probe('orchestrator', orch('/v1/health/readiness')),
        probe('watcher', `${cfg.watcherUrl.replace(/\/$/, '')}/health/readiness`),
      ]);
    },

    async getBundle() {
      const { body } = await requestJson<BundleView>(orch('/v1/ui/bundle'), auth);
      return body;
    },
  };
}
