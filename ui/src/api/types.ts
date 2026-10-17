// Domain types shared by every screen. Field names follow the CUE bundle /
// proto model where one exists, camel-cased for TypeScript.

export type SinkType =
  | 'mongo'
  | 'redis'
  | 'zookeeper'
  | 's3'
  | 'configmap'
  | 'helm'
  | 'vercel'
  | 'linux_server';

export type SyncMode = 'AUTO' | 'GIT_UPSERT_ALL' | 'GIT_SMART_REPAIR' | 'LIVE_STATE';

export type TaskState = 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'RETRYING';

/** Drift state of one environment target, as reported by the watcher. */
export type DriftState = 'in_sync' | 'drifted' | 'healing' | 'heal_failed' | 'not_watched' | 'unknown';

/** Per-file outcome of a sync task (orchestrator SyncResult.status). */
export type FileStatus = 'ok' | 'empty' | 'validation_failed' | 'error' | 'skipped' | 'aborted';

/** What started a task. */
export type TaskTrigger = 'webhook' | 'cli' | 'self_heal' | 'ui';

export interface PruneConfig {
  enabled: boolean;
  last: boolean;
  dryRun: boolean;
  protection: string[];
}

export interface Rule {
  /** `platform/datasource` — the identity the services use for a rule. */
  name: string;
  tenant: string;
  platform: string;
  /** Bundle datasource name: `{type}` or `{type}-{tag}`. */
  datasource: string;
  sinkType: SinkType;
  repositories: string[];
  excludeRepositories: string[];
  fileName?: string;
  filePathMap: Record<string, string>;
  branchMap: Record<string, string>;
  envAsBranch: boolean;
  envAsPr: boolean;
  envAsTags: boolean;
  destinationTemplate: string;
  syncMode: SyncMode;
  selfHeal: boolean;
  dryRun: boolean;
  prune: PruneConfig;
  secretManager?: string;
}

export interface SyncSummary {
  taskId: string;
  state: TaskState;
  trigger: TaskTrigger;
  startedAt: string;
  finishedAt?: string;
  written: number;
  pruned: number;
  message: string;
  /** First file error of a failed sync. */
  error?: string;
}

/** One resolved environment of a rule: where a Git ref lands in the sink. */
export interface Target {
  env: string;
  repository: string;
  /** Git ref that resolves to this environment (branch, tag or `pr-N`). */
  ref: string;
  /** destination_template with {tenant}/{env} substituted. */
  destination: string;
  revision?: string;
  commitMessage?: string;
  author?: string;
  keyCount: number;
  drift: DriftState;
  driftDetectedAt?: string;
  driftedKeys?: number;
  /** Last watcher error: failed snapshot or exhausted heal attempts. */
  driftMessage?: string;
  lastPolledAt?: string;
  lastSync?: SyncSummary;
}

export interface RuleView extends Rule {
  targets: Target[];
}

export interface FileResult {
  file: string;
  env: string;
  status: FileStatus;
  syncMode: string;
  written: number;
  pruned: number;
  rootKey?: string;
  error?: string;
  /** Dry-run only. */
  keys?: number;
  validation?: string;
  sample?: Record<string, string>;
}

export interface Task {
  taskId: string;
  state: TaskState;
  message: string;
  trigger: TaskTrigger;
  rule?: string;
  datasource: string;
  env?: string;
  repository?: string;
  ref?: string;
  revision?: string;
  submittedBy?: string;
  dryRun: boolean;
  createdAt: string;
  finishedAt?: string;
  files: FileResult[];
  /** Live mode: task looked up by id carries only what the server returns. */
  partial?: boolean;
}

export type EventType = 'Normal' | 'Warning';

export type EventReason =
  | 'WebhookReceived'
  | 'SyncStarted'
  | 'SyncSucceeded'
  | 'SyncFailed'
  | 'ValidationFailed'
  | 'KeysPruned'
  | 'DriftDetected'
  | 'HealSucceeded'
  | 'HealFailed'
  | 'SchemaRefreshed';

export interface VtEvent {
  id: string;
  time: string;
  type: EventType;
  reason: EventReason;
  rule?: string;
  env?: string;
  taskId?: string;
  message: string;
}

export type KeyState = 'synced' | 'drifted' | 'missing' | 'unmanaged';

/** One flattened key of a target, Git value next to the live sink value. */
export interface KeyEntry {
  key: string;
  gitValue?: string;
  liveValue?: string;
  secret: boolean;
  protected: boolean;
  state: KeyState;
  sourceFile: string;
}

export type ServiceName = 'gateway' | 'orchestrator' | 'watcher';
export type ServiceStatus = 'up' | 'degraded' | 'down' | 'unknown';

export interface ServiceHealth {
  name: ServiceName;
  status: ServiceStatus;
  checks: Record<string, string>;
  checkedAt: string;
  error?: string;
}

export interface DatasourceView {
  name: string;
  type: SinkType;
  endpoint: string;
  detail: Record<string, string>;
  rules: string[];
}

export interface PlatformView {
  name: string;
  endpoint: string;
  pushEvent: string;
  prEvent: string;
}

export interface BundleView {
  tenant: string;
  platforms: PlatformView[];
  datasources: DatasourceView[];
  secretManagers: { name: string; type: string; endpoint: string }[];
  schemaRegistry?: { platform: string; repo: string; branch: string; files: string[] };
}

export interface SyncRequest {
  rule: string;
  envs: string[];
  dryRun: boolean;
  prune: boolean;
}

export interface ValidateRequest {
  filePath: string;
  content: string;
  datasource?: string;
}

export interface ValidateResult {
  status: 'ok' | 'warn' | 'failed';
  messages: string[];
  keyCount: number;
}

export interface CliSyncRequest {
  datasource: string;
  env: string;
  filePath: string;
  content: string;
  dryRun: boolean;
  label?: string;
}

export interface EventQuery {
  rule?: string;
  limit?: number;
}

export interface TaskQuery {
  rule?: string;
  limit?: number;
}
