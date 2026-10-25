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
  branchAsEnv: boolean;
  prAsEnv: boolean;
  tagAsEnv: boolean;
  destinationTemplate: string;
  syncMode: SyncMode;
  selfHeal: boolean;
  /** Pushes and self-heal skip the rule; syncs started by hand still run. */
  suspended?: boolean;
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
  /** False when the signed-in user can read the rule's repositories but not write to them: dry runs only. */
  canSync?: boolean;
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
  /** The tenant the task ran for; every tenant's tasks come in one list. */
  tenant?: string;
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
  | 'PruneFailed'
  | 'PruneSkipped'
  | 'DriftDetected'
  | 'HealSucceeded'
  | 'HealFailed'
  | 'SchemaRefreshed';

export interface VtEvent {
  id: string;
  /** The tenant the event belongs to; every tenant's events come in one list. */
  tenant?: string;
  time: string;
  type: EventType;
  reason: EventReason;
  rule?: string;
  env?: string;
  taskId?: string;
  message: string;
}

/** unreadable: write-only in the sink (a Vercel sensitive variable); ignored: matches the rule's drift_ignore. */
export type KeyState = 'synced' | 'drifted' | 'missing' | 'unmanaged' | 'unreadable' | 'ignored';

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

/** A rule is identified by its tenant, platform and datasource. */
export interface RuleRef {
  tenant: string;
  /** `platform/datasource`. */
  name: string;
}

export interface DatasourceView {
  name: string;
  type: SinkType;
  endpoint: string;
  detail: Record<string, string>;
  rules: RuleRef[];
}

export interface PlatformView {
  name: string;
  endpoint: string;
  pushEvent: string;
  prEvent: string;
}

/** Rules the signed-in user cannot see for want of a connection: a count per platform, nothing more. */
export interface HiddenRules {
  platform: string;
  rules: number;
}

/** Every rule the viewer may see, of every tenant, and how many more a connection would show. */
export interface RuleList {
  rules: RuleView[];
  hidden: HiddenRules[];
}

export interface BundleView {
  /** The tenants the viewer can see rules of. */
  tenants: string[];
  platforms: PlatformView[];
  datasources: DatasourceView[];
  secretManagers: { name: string; type: string; endpoint: string }[];
  schemaRegistry?: { platform: string; repo: string; branch: string; files: string[] };
}

export interface SyncRequest {
  rule: string;
  /** Needed only when the rule name exists in several tenants. */
  tenant?: string;
  envs: string[];
  dryRun: boolean;
  prune: boolean;
}

export interface ValidateRequest {
  filePath: string;
  content: string;
  datasource?: string;
  /** The tenant whose schema registry checks the file. */
  tenant?: string;
  /** Needed only when rules on several platforms write to the datasource. */
  platform?: string;
}

export interface ValidateResult {
  status: 'ok' | 'warn' | 'failed';
  messages: string[];
  keyCount: number;
}

export interface CliSyncRequest {
  datasource: string;
  /** The tenant the sync is for. */
  tenant?: string;
  env: string;
  filePath: string;
  content: string;
  dryRun: boolean;
  label?: string;
  /** Needed only when rules on several platforms write to the datasource. */
  platform?: string;
}

export interface EventQuery {
  rule?: string;
  /** Narrows to one tenant; the rule name alone can exist in several. */
  tenant?: string;
  limit?: number;
}

export interface TaskQuery {
  rule?: string;
  /** Narrows to one tenant; the rule name alone can exist in several. */
  tenant?: string;
  limit?: number;
}

export type AuthProvider = 'github' | 'gitea';

export interface SessionUser {
  login: string;
  name: string;
  email: string;
  avatarUrl: string;
  isAdmin: boolean;
}

/** A Git platform users can sign in with. */
export interface AuthPlatform {
  /** Bundle platform name, e.g. "gitea" or "github-enterprise". */
  platform: string;
  provider: AuthProvider;
  /** The rest only once signed in. */
  providerUrl?: string;
  /** This session has an account on it. */
  connected?: boolean;
  user?: SessionUser;
  /** Rules of every tenant on this platform (visible ones and not). */
  rules?: number;
}

/** Single sign-on with the Git platforms (GET /v1/auth/session). */
export interface AuthSession {
  /** Sign-in is required; without it every rule is visible to everyone. */
  enabled: boolean;
  authenticated: boolean;
  platforms: AuthPlatform[];
  /** The account signed in with first. */
  user?: SessionUser;
  primary?: string;
  startedAt?: string;
  /** In UI_AUTH_ADMINS: sees every rule. */
  admin?: boolean;
  /** Sign-in is configured but cannot work. */
  error?: string;
}
