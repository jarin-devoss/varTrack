import type { DriftState, FileStatus, RuleView, SyncSummary, Target, TaskState, TaskTrigger } from '../api/types';

/** Visual tone. Maps to the reserved status colors; never used for series. */
export type Tone = 'good' | 'warning' | 'serious' | 'critical' | 'progress' | 'suspended' | 'neutral';

export interface StatusMeta {
  label: string;
  tone: Tone;
  description: string;
}

export const DRIFT: Record<DriftState, StatusMeta> = {
  in_sync: { label: 'In sync', tone: 'good', description: 'The sink matches Git.' },
  drifted: { label: 'Drifted', tone: 'warning', description: 'The sink was changed outside Git. A heal is queued.' },
  healing: { label: 'Healing', tone: 'progress', description: 'Restoring the Git state into the sink.' },
  heal_failed: {
    label: 'Heal failed',
    tone: 'critical',
    description: 'Self-heal gave up after repeated attempts. The sink still differs from Git.',
  },
  unknown: { label: 'Unknown', tone: 'neutral', description: 'The watcher could not read the sink on its last poll.' },
  not_watched: {
    label: 'Not watched',
    tone: 'suspended',
    description: 'Self-heal is off for this rule, so the watcher does not poll it for drift.',
  },
};

export const TASK: Record<TaskState, StatusMeta> = {
  PENDING: { label: 'Pending', tone: 'neutral', description: 'Queued, waiting for a worker.' },
  RUNNING: { label: 'Running', tone: 'progress', description: 'The pipeline is running.' },
  RETRYING: { label: 'Retrying', tone: 'warning', description: 'A step failed and is being retried.' },
  SUCCEEDED: { label: 'Succeeded', tone: 'good', description: 'Finished without errors.' },
  FAILED: { label: 'Failed', tone: 'critical', description: 'Finished with errors.' },
};

export const FILE: Record<FileStatus, StatusMeta> = {
  ok: { label: 'Written', tone: 'good', description: 'Keys were written to the sink.' },
  empty: { label: 'No changes', tone: 'neutral', description: 'Nothing to write.' },
  validation_failed: { label: 'Invalid', tone: 'critical', description: 'Rejected by the CUE schema; nothing was written.' },
  error: { label: 'Error', tone: 'critical', description: 'The write failed.' },
  skipped: { label: 'Skipped', tone: 'neutral', description: 'The file did not match the rule.' },
  aborted: { label: 'Rolled back', tone: 'serious', description: 'Aborted after an earlier file failed; changes were rolled back.' },
};

/** A dry run writes nothing: a file that passed was checked, not written. */
export const FILE_DRY_RUN: Partial<Record<FileStatus, StatusMeta>> = {
  ok: { label: 'Checked', tone: 'good', description: 'Parsed and validated. A dry run writes nothing.' },
};

export const TRIGGER: Record<TaskTrigger, string> = {
  webhook: 'Git push',
  cli: 'CLI',
  self_heal: 'Self-heal',
  ui: 'Manual',
};

/**
 * Sync status compares the current state (the sink) with the desired state
 * (Git): the first thing every box shows. Drift and self-heal details sit
 * underneath it.
 */
export type SyncStatus = 'Synced' | 'OutOfSync' | 'Unknown';

export const SYNC: Record<SyncStatus, StatusMeta> = {
  Synced: { label: 'Synced', tone: 'good', description: 'The current state in the sink matches the desired state in Git.' },
  OutOfSync: { label: 'OutOfSync', tone: 'warning', description: 'The current state in the sink differs from the desired state in Git.' },
  Unknown: {
    label: 'Unknown',
    tone: 'neutral',
    description: "Not compared: self-heal is off for this rule, or the watcher couldn't read the sink.",
  },
};

export function targetSync(t: Target): SyncStatus {
  if (t.drift === 'in_sync') return 'Synced';
  if (t.drift === 'drifted' || t.drift === 'healing' || t.drift === 'heal_failed') return 'OutOfSync';
  return 'Unknown';
}

/** OutOfSync if any environment is; Synced if every compared one is. */
export function ruleSync(rule: RuleView): SyncStatus {
  const states = rule.targets.map(targetSync);
  if (states.includes('OutOfSync')) return 'OutOfSync';
  if (states.includes('Synced')) return 'Synced';
  return 'Unknown';
}

/** Short words for what is going on with one environment, beyond its sync status. */
export function targetNote(t: Target): string {
  if (t.drift === 'heal_failed') return 'self-heal gave up';
  if (t.drift === 'healing') return 'self-heal in progress';
  if (t.drift === 'drifted') return t.driftedKeys ? (t.driftedKeys === 1 ? '1 key differs' : `${t.driftedKeys} keys differ`) : 'differs from Git';
  if (t.drift === 'unknown') return "watcher can't read the sink";
  if (t.lastSync?.state === 'FAILED') return 'last sync failed';
  if (t.lastSync?.state === 'RUNNING' || t.lastSync?.state === 'PENDING') return 'syncing';
  if (t.drift === 'not_watched') return 'not watched';
  return 'matches Git';
}

/** Worst first, for sorting and roll-ups. */
export const DRIFT_SEVERITY: DriftState[] = ['heal_failed', 'drifted', 'unknown', 'healing', 'in_sync', 'not_watched'];

/**
 * Segment order of the drift bar. Chosen so that no two neighbouring
 * segments are hard to tell apart (checked for color-vision deficiency in
 * both themes); unwatched targets are left out of the bar entirely.
 */
export const DRIFT_BAR_ORDER: DriftState[] = ['healing', 'heal_failed', 'drifted', 'in_sync', 'unknown'];

export function worstDrift(states: DriftState[]): DriftState {
  if (states.length === 0) return 'not_watched';
  return DRIFT_SEVERITY.find(s => states.includes(s)) ?? 'not_watched';
}

export function ruleDrift(rule: RuleView): DriftState {
  return worstDrift(rule.targets.map(t => t.drift));
}

export function latestSync(targets: Target[]): SyncSummary | undefined {
  return targets
    .map(t => t.lastSync)
    .filter((s): s is SyncSummary => !!s)
    .sort((a, b) => b.startedAt.localeCompare(a.startedAt))[0];
}

/**
 * The rule-level sync state: a running sync wins, then any failed target
 * (a later success elsewhere doesn't hide it), then the latest result.
 */
export function ruleSyncState(rule: RuleView): TaskState | 'never' {
  const states = rule.targets.map(t => t.lastSync?.state).filter((s): s is TaskState => !!s);
  if (states.length === 0) return 'never';
  for (const s of ['RUNNING', 'PENDING', 'RETRYING', 'FAILED'] as TaskState[]) {
    if (states.includes(s)) return s;
  }
  return 'SUCCEEDED';
}

export function isActive(state: TaskState): boolean {
  return state === 'PENDING' || state === 'RUNNING' || state === 'RETRYING';
}

export interface Attention {
  rule: RuleView;
  target: Target;
  kind: 'drift' | 'sync';
  tone: Tone;
  title: string;
  detail: string;
  at?: string;
}

/** Targets that need a human, worst first. */
export function needsAttention(rules: RuleView[]): Attention[] {
  const items: Attention[] = [];
  for (const rule of rules) {
    for (const target of rule.targets) {
      if (target.drift === 'heal_failed' || target.drift === 'drifted' || target.drift === 'unknown') {
        const meta = DRIFT[target.drift];
        const keys = target.driftedKeys ? `${target.driftedKeys} key${target.driftedKeys === 1 ? '' : 's'} differ from Git. ` : '';
        items.push({
          rule,
          target,
          kind: 'drift',
          tone: meta.tone,
          title: meta.label,
          detail: `${keys}${target.driftMessage ?? meta.description}`,
          at: target.driftDetectedAt ?? target.lastPolledAt,
        });
      }
      if (target.lastSync?.state === 'FAILED' && target.drift !== 'heal_failed') {
        items.push({
          rule,
          target,
          kind: 'sync',
          tone: 'critical',
          title: 'Last sync failed',
          detail: target.lastSync.error ?? target.lastSync.message,
          at: target.lastSync.finishedAt ?? target.lastSync.startedAt,
        });
      }
    }
  }
  const rank = (a: Attention) => (a.kind === 'drift' ? DRIFT_SEVERITY.indexOf(a.target.drift) : 0.5);
  return items.sort((a, b) => rank(a) - rank(b) || (b.at ?? '').localeCompare(a.at ?? ''));
}
