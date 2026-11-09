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

/** What a sync did to the keys: "19 keys written", "3 keys written, 812 unchanged", or (a smart repair that found everything in place) "nothing to change (812 keys already match Git)". */
export function writtenSentence(written: number, unchanged: number): string {
  if (!written && unchanged) return `nothing to change (${keys(unchanged)} already match${unchanged === 1 ? 'es' : ''} Git)`;
  return `${keys(written)} written${unchanged ? `, ${unchanged} unchanged` : ''}`;
}

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
  const unchanged = tasks.reduce((n, t) => n + t.files.reduce((m, f) => m + (f.unchanged ?? 0), 0), 0);
  const pruned = tasks.reduce((n, t) => n + t.files.reduce((m, f) => m + f.pruned, 0), 0);
  return dryRun
    ? { tone: 'good', title: `Dry run of ${what} passed`, detail: `${keys(checked)} checked, nothing written`, taskId: tasks[0]?.taskId }
    : {
        tone: 'good',
        title: `Synced ${what}`,
        detail: `${writtenSentence(written, unchanged)}${pruned ? `, ${keys(pruned)} deleted` : ''}`,
        taskId: tasks[0]?.taskId,
      };
}

/**
 * Whether a failed run failed because the schema rejected a file: a file the pipeline marked so, or (when a rule with
 * strict_validation stops at the file, which leaves no file result) the task's own message saying so.
 */
export function isValidationFailure(task: Task): boolean {
  return (
    task.state === 'FAILED' &&
    (task.files.some(f => f.status === 'validation_failed') || /strict_validation|validation failed/i.test([task.message, ...task.files.map(f => f.error ?? '')].join('\n')))
  );
}

/** What the schema objected to: the part after "strict_validation:" when the message has one, else its first line. */
function validationReason(text: string): string {
  const after = /strict_validation:\s*([\s\S]*)/.exec(text);
  return firstLine(after ? after[1] : text) || 'the file does not match its schema';
}

/** A finished dry run in one line: what a sync of the same files would do. */
export interface DryRunVerdict {
  tone: 'good' | 'warning' | 'critical';
  text: string;
  /** Totals over the files the sink could be compared for; absent when none could. */
  totals?: { added: number; changed: number; deleted: number };
}

/**
 * "A sync of default would add 2 keys, change 1 and delete none." · "default already matches Git — a sync would change
 * nothing." · "Validation failed: … — a sync would be refused." · or why what a sync would change is not known.
 */
export function dryRunVerdict(task: Task): DryRunVerdict {
  const files = task.files.filter(f => f.env);
  const what = environmentsLabel([...new Set(files.map(f => f.env))].length ? [...new Set(files.map(f => f.env))] : [task.env ?? 'the rule']);
  if (task.state === 'FAILED') {
    const bad = task.files.find(f => f.status === 'validation_failed' || f.error);
    const text = bad?.error ?? bad?.validationError ?? task.message;
    if (isValidationFailure(task)) {
      return { tone: 'critical', text: `Validation failed: ${validationReason(text)} — a sync would be refused.` };
    }
    return { tone: 'critical', text: `The dry run failed: ${firstLine(text) || 'The task failed.'}` };
  }
  const compared = files.filter(f => f.wouldChange && !f.wouldChange.error);
  if (files.length === 0 || compared.length < files.length) {
    const why = files.length === 0 ? 'No file was read.' : files.find(f => f.wouldChange?.error)?.wouldChange?.error;
    return {
      tone: 'warning',
      text: files.length === 0
        ? 'No file was read, so there is nothing a sync would change.'
        : `Git was read and checked against its schema, but ${why ? `the sink could not be compared (${firstLine(why)})` : 'the sink was not compared'}, so what a sync would change is not known.`,
    };
  }
  const added = compared.reduce((n, f) => n + (f.wouldChange?.addedCount ?? f.wouldChange?.added?.length ?? 0), 0);
  const changed = compared.reduce((n, f) => n + (f.wouldChange?.changedCount ?? f.wouldChange?.changed?.length ?? 0), 0);
  const deleted = Object.values(task.wouldPrune ?? {}).reduce((n, ks) => n + ks.length, 0);
  const totals = { added, changed, deleted };
  if (added + changed + deleted === 0) {
    const several = new Set(files.map(f => f.env)).size > 1;
    return { tone: 'good', text: `${what} ${several ? 'already match' : 'already matches'} Git — a sync would change nothing.`, totals };
  }
  const parts = [added ? `add ${keys(added)}` : 'add none', changed ? `change ${changed}` : 'change none', deleted ? `delete ${keys(deleted)}` : 'delete none'];
  return { tone: 'warning', text: `A sync of ${what} would ${parts[0]}, ${parts[1]} and ${parts[2]}.`, totals };
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
