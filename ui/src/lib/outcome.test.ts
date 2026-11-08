import { describe, expect, it } from 'vitest';
import type { FileResult, Task } from '../api/types';
import { environmentsLabel, pruneImpact, someKeys, syncOutcome } from './outcome';

const file = (over: Partial<FileResult> = {}): FileResult =>
  ({ file: 'app.yaml', env: 'staging', status: 'ok', syncMode: '', written: 0, pruned: 0, ...over }) as FileResult;
const task = (over: Partial<Task> = {}): Task =>
  ({ taskId: 't1', state: 'SUCCEEDED', message: '', trigger: 'ui', datasource: 'redis', dryRun: false, createdAt: '', files: [], ...over }) as Task;

describe('environmentsLabel', () => {
  it('names one environment and counts several', () => {
    expect(environmentsLabel(['staging'])).toBe('staging');
    expect(environmentsLabel(['staging', 'production'])).toBe('2 environments');
  });
});

describe('syncOutcome', () => {
  it('says what a sync wrote', () => {
    const out = syncOutcome([task({ files: [file({ written: 19 })] })], ['staging'], false);
    expect(out).toMatchObject({ tone: 'good', title: 'Synced staging', detail: '19 keys written', taskId: 't1' });
  });

  it('adds up several environments and says what was deleted', () => {
    const out = syncOutcome(
      [task({ taskId: 'a', files: [file({ written: 19, pruned: 2 })] }), task({ taskId: 'b', files: [file({ written: 1 })] })],
      ['staging', 'production'],
      false,
    );
    expect(out).toMatchObject({ title: 'Synced 2 environments', detail: '20 keys written, 2 keys deleted' });
  });

  it('says a dry run checked and wrote nothing', () => {
    const out = syncOutcome([task({ dryRun: true, files: [file({ keys: 1 })] })], ['staging'], true);
    expect(out).toMatchObject({ tone: 'good', title: 'Dry run of staging passed', detail: '1 key checked, nothing written' });
  });

  it('reports the first failure with its reason, and links to that task', () => {
    const out = syncOutcome(
      [task({ taskId: 'a' }), task({ taskId: 'b', state: 'FAILED', message: 'failed: 1 file(s)\nTraceback…', files: [file({ error: 'Redis refused 1 of 2 commands' })] })],
      ['staging', 'production'],
      false,
    );
    expect(out).toEqual({ tone: 'critical', title: 'Sync of 2 environments failed', detail: 'Redis refused 1 of 2 commands', taskId: 'b' });
  });

  it("uses a failed task's message (its first line) when no file has an error", () => {
    const out = syncOutcome([task({ state: 'FAILED', message: 'strict_validation: bad value\n  at line 3' })], ['staging'], true);
    expect(out).toMatchObject({ tone: 'critical', title: 'Dry run of staging failed', detail: 'strict_validation: bad value' });
  });
});

describe('pruneImpact', () => {
  it('counts the keys of the selected environments that would be deleted', () => {
    const impact = pruneImpact({ production: ['a', 'b'], staging: ['c'], qa: [] }, ['production', 'qa']);
    expect(impact).toEqual({ total: 2, byEnv: [['production', ['a', 'b']]] });
  });

  it('says nothing is deleted when nothing is', () => {
    expect(pruneImpact({}, ['production'])).toEqual({ total: 0, byEnv: [] });
  });
});

describe('someKeys', () => {
  it('lists a few and counts the rest', () => {
    expect(someKeys(['a', 'b'])).toBe('a, b');
    expect(someKeys(['a', 'b', 'c', 'd', 'e', 'f', 'g'], 5)).toBe('a, b, c, d, e and 2 more');
  });
});
