import type { Task } from '../api/types';

/** What to tell a person once the tasks of the sync they started have ended. */
export interface SyncOutcome {
  tone: 'good' | 'critical';
  title: string;
  /** Plain text under the title: what was written, or why it failed. */
  detail: string;
  /** The task to open for more: the first that failed, else the only one. */
  taskId?: string;
}

/** "staging", "2 environments". */
export function environmentsLabel(envs: string[]): string {
  return envs.length === 1 ? envs[0] : `${envs.length} environments`;
}

const keys = (n: number) => `${n} key${n === 1 ? '' : 's'}`;

/** The first line of a message (a task's message can carry a whole traceback). */
function firstLine(message: string): string {
  const line = message.split('\n')[0].trim();
  return line.length > 240 ? `${line.slice(0, 237)}…` : line;
}

/**
 * The sentence for a sync that has ended: "Synced staging · 19 keys written", "Dry run of 2 environments passed · 38 keys
 * checked, nothing written", or "Sync of staging failed" with the reason. *tasks* are the tasks it started, finished.
 */
export function syncOutcome(tasks: Task[], envs: string[], dryRun: boolean): SyncOutcome {
  const what = environmentsLabel(envs);
  const failed = tasks.find(t => t.state === 'FAILED');
  if (failed) {
    const error = failed.files.find(f => f.error)?.error;
    return {
      tone: 'critical',
      title: `${dryRun ? 'Dry run' : 'Sync'} of ${what} failed`,
      detail: firstLine(error ?? failed.message) || 'The task failed.',
      taskId: failed.taskId,
    };
  }
  const written = tasks.reduce((n, t) => n + t.files.reduce((m, f) => m + f.written, 0), 0);
  const checked = tasks.reduce((n, t) => n + t.files.reduce((m, f) => m + (f.keys ?? 0), 0), 0);
  const pruned = tasks.reduce((n, t) => n + t.files.reduce((m, f) => m + f.pruned, 0), 0);
  return dryRun
    ? { tone: 'good', title: `Dry run of ${what} passed`, detail: `${keys(checked)} checked, nothing written`, taskId: tasks[0]?.taskId }
    : {
        tone: 'good',
        title: `Synced ${what}`,
        detail: `${keys(written)} written${pruned ? `, ${keys(pruned)} deleted` : ''}`,
        taskId: tasks[0]?.taskId,
      };
}

/** What a sync of some environments would delete, from a dry run's report. */
export interface PruneImpact {
  total: number;
  /** The environments that would lose keys, with the keys. */
  byEnv: [env: string, keys: string[]][];
}

export function pruneImpact(wouldPrune: Record<string, string[]>, envs: string[]): PruneImpact {
  const byEnv = envs.flatMap((env): [string, string[]][] => (wouldPrune[env]?.length ? [[env, wouldPrune[env]]] : []));
  return { total: byEnv.reduce((n, [, ks]) => n + ks.length, 0), byEnv };
}

/** "a, b, c and 4 more": the first *max* keys, then how many are left. */
export function someKeys(list: string[], max = 5): string {
  return list.length <= max ? list.join(', ') : `${list.slice(0, max).join(', ')} and ${list.length - max} more`;
}

export const keyCount = keys;
