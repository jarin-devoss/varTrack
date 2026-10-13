import type { DriftState, FileStatus, TaskState } from '../api/types';
import { DRIFT, FILE, FILE_DRY_RUN, SYNC, TASK, type SyncStatus, type Tone } from '../lib/status';
import { Icon, type IconName } from './Icon';

/** Status tone → color name used by the pill, dot and text classes. */
export const TONE_COLOR: Record<Tone, string> = {
  good: 'green',
  warning: 'yellow',
  serious: 'orange',
  critical: 'red',
  progress: 'blue',
  suspended: 'gray',
  neutral: 'gray',
};

const DRIFT_ICON: Record<DriftState, IconName> = {
  in_sync: 'checkCircle',
  drifted: 'arrowUpCircle',
  healing: 'loader',
  heal_failed: 'xCircle',
  unknown: 'helpCircle',
  not_watched: 'pauseCircle',
};

const TASK_ICON: Record<TaskState, IconName> = {
  PENDING: 'clock',
  RUNNING: 'loader',
  RETRYING: 'refresh',
  SUCCEEDED: 'checkCircle',
  FAILED: 'xCircle',
};

const FILE_ICON: Record<FileStatus, IconName> = {
  ok: 'checkCircle',
  empty: 'minusCircle',
  validation_failed: 'xCircle',
  error: 'xCircle',
  skipped: 'minusCircle',
  aborted: 'alertCircle',
};

export function driftIcon(state: DriftState): IconName {
  return DRIFT_ICON[state];
}

export function taskIcon(state: TaskState): IconName {
  return TASK_ICON[state];
}

/** A colored status icon; pass title when it stands without a text label. */
export function StatusIcon({ icon, tone, spin, size = 16, title }: { icon: IconName; tone: Tone; spin?: boolean; size?: number; title?: string }) {
  return <Icon name={icon} size={size} className={`c-${TONE_COLOR[tone]}${spin ? ' spin' : ''}`} strokeWidth={2.1} label={title} style={{ flex: 'none' }} />;
}

function Pill({ tone, label, description, pulse, large }: { tone: Tone; label: string; description: string; pulse?: boolean; large?: boolean }) {
  return (
    <span className={`pill pill--${TONE_COLOR[tone]}${pulse ? ' pill--pulse' : ''}${large ? ' pill--lg' : ''}`} title={description}>
      {label}
    </span>
  );
}

export function DriftPill({ state, large }: { state: DriftState; large?: boolean }) {
  const m = DRIFT[state];
  return <Pill tone={m.tone} label={m.label} description={m.description} pulse={state === 'healing'} large={large} />;
}

export function TaskPill({ state, large }: { state: TaskState; large?: boolean }) {
  const m = TASK[state];
  return <Pill tone={m.tone} label={m.label} description={m.description} pulse={state === 'RUNNING'} large={large} />;
}

export function FilePill({ status, dryRun = false }: { status: FileStatus; dryRun?: boolean }) {
  const m = (dryRun && FILE_DRY_RUN[status]) || FILE[status];
  return <Pill tone={m.tone} label={m.label} description={m.description} />;
}

/** Icon-only status mark, for dense rows. */
export function DriftIcon({ state, size = 16 }: { state: DriftState; size?: number }) {
  const m = DRIFT[state];
  return <StatusIcon icon={DRIFT_ICON[state]} tone={m.tone} spin={state === 'healing'} size={size} title={m.label} />;
}

export function TaskIcon({ state, size = 16 }: { state: TaskState; size?: number }) {
  const m = TASK[state];
  return <StatusIcon icon={TASK_ICON[state]} tone={m.tone} spin={state === 'RUNNING' || state === 'RETRYING'} size={size} title={m.label} />;
}

export function FileIcon({ status, size = 16 }: { status: FileStatus; size?: number }) {
  const m = FILE[status];
  return <StatusIcon icon={FILE_ICON[status]} tone={m.tone} size={size} title={m.label} />;
}

const SYNC_ICON: Record<SyncStatus, IconName> = { Synced: 'checkCircle', OutOfSync: 'arrowUpCircle', Unknown: 'helpCircle' };

export function syncIcon(s: SyncStatus): IconName {
  return SYNC_ICON[s];
}

/** Current vs desired: Synced / OutOfSync / Unknown, with its icon. */
export function SyncPill({ status, large }: { status: SyncStatus; large?: boolean }) {
  const m = SYNC[status];
  return (
    <span className={`pill pill--${TONE_COLOR[m.tone]}${large ? ' pill--lg' : ''}`} title={m.description}>
      <Icon name={SYNC_ICON[status]} size={large ? 15 : 13} strokeWidth={2.2} />
      {m.label}
    </span>
  );
}

export function SyncIcon({ status, size = 16 }: { status: SyncStatus; size?: number }) {
  const m = SYNC[status];
  return <StatusIcon icon={SYNC_ICON[status]} tone={m.tone} size={size} title={m.label} />;
}

/** Last sync result with its icon, e.g. "✓ Sync succeeded". */
export function OpPill({ state, large }: { state: TaskState; large?: boolean }) {
  const m = TASK[state];
  return (
    <span className={`pill pill--${TONE_COLOR[m.tone]}${state === 'RUNNING' ? ' pill--pulse' : ''}${large ? ' pill--lg' : ''}`} title={`Last sync: ${m.description}`}>
      <Icon name={TASK_ICON[state]} size={large ? 15 : 13} strokeWidth={2.2} className={state === 'RUNNING' || state === 'RETRYING' ? 'spin' : undefined} />
      Sync {m.label.toLowerCase()}
    </span>
  );
}
