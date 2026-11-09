import { describe, expect, it } from 'vitest';
import type { FileResult, Task } from '../api/types';
import { dryRunVerdict, environmentsLabel, isValidationFailure, pruneImpact, someKeys, syncOutcome, writtenSentence } from './outcome';

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

  it('says how many keys a smart repair left alone', () => {
    const out = syncOutcome([task({ files: [file({ written: 3, unchanged: 812 })] })], ['staging'], false);
    expect(out).toMatchObject({ title: 'Synced staging', detail: '3 keys written, 812 unchanged' });
  });

  it('says nothing needed changing, not "0 keys written"', () => {
    const out = syncOutcome([task({ files: [file({ written: 0, unchanged: 812 })] })], ['staging'], false);
    expect(out).toMatchObject({ title: 'Synced staging', detail: 'nothing to change (812 keys already match Git)' });
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

describe('dryRunVerdict', () => {
  const ran = (change: object, over: Partial<FileResult> = {}) => file({ env: 'default', keys: 10, ...over, wouldChange: change });

  it('says what a sync would add, change and delete', () => {
    const out = dryRunVerdict(task({ dryRun: true, wouldPrune: { default: ['old'] }, files: [ran({ addedCount: 2, changedCount: 1 })] }));
    expect(out).toMatchObject({ tone: 'warning', text: 'A sync of default would add 2 keys, change 1 and delete 1 key.', totals: { added: 2, changed: 1, deleted: 1 } });
  });

  it('says none for what it would not do', () => {
    expect(dryRunVerdict(task({ files: [ran({ addedCount: 2, changedCount: 0 })] })).text).toBe('A sync of default would add 2 keys, change none and delete none.');
  });

  it('says a sink that already matches Git would change nothing', () => {
    const out = dryRunVerdict(task({ files: [ran({ addedCount: 0, changedCount: 0, unchanged: 10 })] }));
    expect(out).toMatchObject({ tone: 'good', text: 'default already matches Git — a sync would change nothing.' });
  });

  it('adds up several environments', () => {
    const out = dryRunVerdict(task({ files: [ran({ addedCount: 1, changedCount: 0 }), ran({ addedCount: 0, changedCount: 3 }, { env: 'staging' })] }));
    expect(out.text).toBe('A sync of 2 environments would add 1 key, change 3 and delete none.');
    expect(dryRunVerdict(task({ files: [ran({ addedCount: 0, changedCount: 0 }), ran({ addedCount: 0, changedCount: 0 }, { env: 'staging' })] })).text).toBe(
      '2 environments already match Git — a sync would change nothing.',
    );
  });

  it('says a sync would be refused when the schema rejected the file, with the reason', () => {
    const out = dryRunVerdict(task({ state: 'FAILED', files: [file({ status: 'validation_failed', error: 'api.rate_limit: out of range\nmore' })] }));
    expect(out).toEqual({ tone: 'critical', text: 'Validation failed: api.rate_limit: out of range — a sync would be refused.' });
  });

  it('says the schema rejected the file when a rule with strict_validation stopped at it and left no file result', () => {
    const out = dryRunVerdict(
      task({
        state: 'FAILED',
        message: 'failed: 1 file(s) could not be processed (app.yaml: strict_validation: CUE validation failed for app.yaml: "log_level": invalid value "VERBOSE"):\nmore',
      }),
    );
    expect(out).toEqual({ tone: 'critical', text: 'Validation failed: CUE validation failed for app.yaml: "log_level": invalid value "VERBOSE"): — a sync would be refused.' });
  });

  it('says why a task failed when it was not the schema', () => {
    expect(dryRunVerdict(task({ state: 'FAILED', message: 'gitea/s3 could not read the repository' })).text).toBe('The dry run failed: gitea/s3 could not read the repository');
  });

  it('says when the sink could not be compared, and not that nothing would change', () => {
    const out = dryRunVerdict(task({ files: [ran({ error: "can't connect to redis: refused" })] }));
    expect(out.tone).toBe('warning');
    expect(out.text).toContain("the sink could not be compared (can't connect to redis: refused)");
    expect(out.totals).toBeUndefined();
  });

  it('says when no file was read', () => {
    expect(dryRunVerdict(task({ files: [] })).text).toBe('No file was read, so there is nothing a sync would change.');
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

describe('isValidationFailure', () => {
  it('is a failed task the schema rejected, by its file or by its message, and nothing else', () => {
    expect(isValidationFailure(task({ state: 'FAILED', files: [file({ status: 'validation_failed' })] }))).toBe(true);
    expect(isValidationFailure(task({ state: 'FAILED', message: 'app.yaml: strict_validation: bad' }))).toBe(true);
    expect(isValidationFailure(task({ state: 'FAILED', message: 'gitea/s3 could not read the repository' }))).toBe(false);
    expect(isValidationFailure(task({ state: 'SUCCEEDED', message: 'strict_validation' }))).toBe(false);
  });
});

describe('writtenSentence', () => {
  it('counts what was written and what was left alone', () => {
    expect(writtenSentence(19, 0)).toBe('19 keys written');
    expect(writtenSentence(1, 0)).toBe('1 key written');
    expect(writtenSentence(3, 812)).toBe('3 keys written, 812 unchanged');
    expect(writtenSentence(0, 812)).toBe('nothing to change (812 keys already match Git)');
    expect(writtenSentence(0, 1)).toBe('nothing to change (1 key already matches Git)');
    expect(writtenSentence(0, 0)).toBe('0 keys written');
  });
});
