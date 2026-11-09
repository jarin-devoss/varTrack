import { Link } from 'react-router-dom';
import type { FileResult, Task, WouldChange } from '../api/types';
import { Notice, Revision, Timestamp } from '../components/common';
import { FilePill } from '../components/Status';
import { dryRunVerdict, isValidationFailure, keyCount, someKeys } from '../lib/outcome';
import { isActive } from '../lib/status';
import { taskHref } from './links';
import { StageStrip, type StageState } from './tasks';

/** The stages of a dry run, in the order it does them; the last is never done: a dry run writes nothing. */
export function dryRunStages(task: Task): { label: string; state: StageState }[] {
  const s = (read: StageState, parse: StageState, compare: StageState) => [
    { label: 'Read from Git', state: read },
    { label: 'Parse & validate', state: parse },
    { label: 'Compare with the sink', state: compare },
    { label: 'Write to sink (skipped: dry run)', state: 'skipped' as const },
  ];
  const compared = task.files.length > 0 && task.files.every(f => f.wouldChange && !f.wouldChange.error);
  switch (task.state) {
    case 'PENDING':
      return s('active', 'pending', 'pending');
    case 'RUNNING':
    case 'RETRYING':
      return task.files.length ? s('done', 'done', 'active') : s('done', 'active', 'pending');
    case 'SUCCEEDED':
      return s('done', 'done', compared ? 'done' : 'skipped');
    case 'FAILED':
      // The schema (or the parse) rejected a file: it was read. Nothing was read at all (no file, no such branch): reading failed.
      return task.files.length === 0 && !isValidationFailure(task) ? s('failed', 'skipped', 'skipped') : s('done', 'failed', 'skipped');
  }
}

const VALIDATION: Record<string, string> = {
  ok: 'passed its schema',
  warn: 'failed its schema (the rule does not require it, so a sync would still write it)',
  failed: 'failed its schema',
  none: 'no schema checked it',
};

function KeyList({ label, keys, count, testId }: { label: string; keys: string[]; count: number; testId: string }) {
  if (count === 0) return null;
  return (
    <div data-testid={testId}>
      <strong>
        {label} ({count})
      </strong>
      <ul className="dryrun__keys">
        {keys.map(k => (
          <li key={k}>
            <code>{k}</code>
          </li>
        ))}
        {count > keys.length && <li className="muted">and {count - keys.length} more</li>}
      </ul>
    </div>
  );
}

/** What a sync of one file would add and change in its environment (a dry run's `wouldChange`). */
export function Changes({ change }: { change: WouldChange }) {
  if (change.error) {
    return <Notice tone="warning">The sink could not be compared: {change.error}. The file was read and checked; what a sync would change is not known.</Notice>;
  }
  const added = change.addedCount ?? change.added?.length ?? 0;
  const changed = change.changedCount ?? change.changed?.length ?? 0;
  return (
    <div className="stack stack--tight" data-testid="would-change">
      <span className="section-label">What a sync would change</span>
      <p className="secondary">
        {keyCount(added)} to add, {keyCount(changed)} to change, {keyCount(change.unchanged ?? 0)} already the same
        {change.secrets ? `, ${keyCount(change.secrets)} secret (not compared)` : ''}
        {change.unreadable ? `, ${keyCount(change.unreadable)} the sink cannot show` : ''}.
      </p>
      <KeyList label="Would be added" keys={change.added ?? []} count={added} testId="would-add" />
      {changed > 0 && (
        <div data-testid="would-change-keys">
          <strong>Would be changed ({changed})</strong>
          <table className="table table--compact">
            <thead>
              <tr>
                <th>Key</th>
                <th>In the sink now</th>
                <th>In Git</th>
              </tr>
            </thead>
            <tbody>
              {(change.changed ?? []).map(c => (
                <tr key={c.key}>
                  <td>
                    <code>{c.key}</code>
                  </td>
                  <td className="mono">{c.from}</td>
                  <td className="mono">{c.to}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {changed > (change.changed?.length ?? 0) && <p className="muted">and {changed - (change.changed?.length ?? 0)} more</p>}
        </div>
      )}
    </div>
  );
}

function FileReport({ file: f, dryRun }: { file: FileResult; dryRun: boolean }) {
  return (
    <section className="card card__body stack" aria-label={`${f.file} in ${f.env}`}>
      <div className="row row--wrap" style={{ gap: 10 }}>
        <span className="mono">{f.file}</span>
        <span className="tag">{f.env}</span>
        <FilePill status={f.status} dryRun={dryRun} />
        <span className="muted">{f.keys !== undefined ? keyCount(f.keys) : '—'} read</span>
      </div>
      {f.validation && (
        <p className="secondary" data-testid="schema-result">
          {f.schema && f.schema !== 'none' ? (
            <>
              Schema <code>{f.schema}</code>
              {f.schemaSource ? <span className="muted"> ({f.schemaSource})</span> : null}:{' '}
            </>
          ) : null}
          {VALIDATION[f.validation] ?? f.validation}.{f.schemaNote ? <span className="muted"> {f.schemaNote}</span> : null}
        </p>
      )}
      {(f.validationError || f.error) && (
        <Notice tone={f.status === 'validation_failed' || f.error ? 'critical' : 'warning'}>
          <pre className="code" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
            {f.error ?? f.validationError}
          </pre>
        </Notice>
      )}
      {f.wouldChange ? (
        <Changes change={f.wouldChange} />
      ) : f.sample ? (
        <details>
          <summary className="muted" style={{ cursor: 'pointer' }}>
            Sample values
          </summary>
          <pre className="code" style={{ marginTop: 6 }}>
            {Object.entries(f.sample).map(([k, v]) => `${k} = ${v}`).join('\n')}
          </pre>
        </details>
      ) : null}
    </section>
  );
}

/**
 * One dry run, as a report: the stages it went through, the file and schema result for each file, what a sync would add,
 * change and delete, and the outcome in one line. The dry-run page and a dry-run task's page show the same thing.
 */
export function DryRunReport({ task: t }: { task: Task }) {
  const active = isActive(t.state);
  const verdict = active ? undefined : dryRunVerdict(t);
  const prune = Object.entries(t.wouldPrune ?? {}).filter(([, ks]) => ks.length > 0);
  return (
    <div className="stack" data-testid="dryrun-report" aria-label={`Dry run of ${t.env ?? t.rule ?? 'the rule'}`}>
      <div className="row row--wrap" style={{ gap: 10 }}>
        <strong>{t.env ?? '—'}</strong>
        <span className="muted">
          <Timestamp iso={t.createdAt} />
          {t.submittedBy ? ` · ${t.submittedBy}` : ''}
        </span>
        <span className="spacer" />
        <Link to={taskHref(t.taskId)} className="mono">
          Task {t.taskId.slice(0, 8)}
        </Link>
      </div>
      <StageStrip stages={dryRunStages(t)} />
      <p className="secondary">
        Read <span className="mono">{t.repository ? `${t.repository}@${t.ref ?? ''}` : t.ref ?? '—'}</span>
        {t.revision ? (
          <>
            {' '}
            at <Revision sha={t.revision} />
          </>
        ) : null}
        .
      </p>
      {active && <p className="muted">The dry run is {t.state === 'PENDING' ? 'queued' : 'running'}…</p>}
      {verdict && (
        <Notice tone={verdict.tone === 'critical' ? 'critical' : verdict.tone === 'good' ? 'good' : 'info'}>
          <span data-testid="dryrun-verdict">{verdict.text}</span>
        </Notice>
      )}
      {t.files.map((f, i) => (
        <FileReport key={`${f.file}:${f.env}:${i}`} file={f} dryRun />
      ))}
      {/* A run that stopped at a file leaves the reason only in its message: shown in full, as it was said. */}
      {t.state === 'FAILED' && t.message && !t.files.some(f => f.error || f.validationError) && (
        <pre className="code" data-testid="dryrun-error" style={{ margin: 0, whiteSpace: 'pre-wrap', maxHeight: 240, overflowY: 'auto' }}>
          {t.message}
        </pre>
      )}
      {prune.map(([env, keys]) => (
        <div key={env} data-testid="would-delete">
          <strong>
            Would be deleted from {env} ({keys.length})
          </strong>
          <p className="secondary">
            <code>{someKeys(keys, 20)}</code>
          </p>
        </div>
      ))}
    </div>
  );
}
