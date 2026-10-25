// Small builders for unit tests. Imported only by *.test.ts files, so none of
// this ends up in the app bundle.
import type { KeyEntry, RuleView, SyncSummary, Target, Task } from '../api/types';

export function makeRule(overrides: Partial<RuleView> = {}): RuleView {
  const platform = overrides.platform ?? 'github';
  const datasource = overrides.datasource ?? 'mongo';
  return {
    name: `${platform}/${datasource}`,
    tenant: 'default',
    platform,
    datasource,
    sinkType: 'mongo',
    repositories: ['acme/configs'],
    excludeRepositories: [],
    fileName: 'app.yaml',
    filePathMap: {},
    branchMap: { main: 'production', develop: 'staging' },
    branchAsEnv: false,
    prAsEnv: false,
    tagAsEnv: false,
    destinationTemplate: '{env}-config',
    syncMode: 'AUTO',
    selfHeal: true,
    dryRun: false,
    prune: { enabled: false, dryRun: false, protection: [] },
    targets: [],
    ...overrides,
  };
}

export function makeTarget(overrides: Partial<Target> = {}): Target {
  return { env: 'production', repository: 'acme/configs', ref: 'main', destination: 'production-config', keyCount: 3, drift: 'in_sync', ...overrides };
}

export function makeSync(overrides: Partial<SyncSummary> = {}): SyncSummary {
  return { taskId: 't1', state: 'SUCCEEDED', trigger: 'webhook', startedAt: '2026-10-11T10:00:00Z', written: 3, pruned: 0, message: '', ...overrides };
}

export function makeKey(key: string, gitValue: string | undefined, liveValue: string | undefined, overrides: Partial<KeyEntry> = {}): KeyEntry {
  const state = gitValue === undefined ? 'unmanaged' : liveValue === undefined ? 'missing' : gitValue === liveValue ? 'synced' : 'drifted';
  return { key, gitValue, liveValue, secret: false, protected: false, state, sourceFile: 'app.yaml', ...overrides };
}

export function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    taskId: 't1',
    state: 'SUCCEEDED',
    message: '',
    trigger: 'webhook',
    datasource: 'mongo',
    env: 'production',
    dryRun: false,
    createdAt: '2026-10-11T10:00:00Z',
    finishedAt: '2026-10-11T10:00:02Z',
    files: [{ file: 'app.yaml', env: 'production', status: 'ok', syncMode: 'GIT_UPSERT_ALL', written: 3, pruned: 0 }],
    ...overrides,
  };
}
