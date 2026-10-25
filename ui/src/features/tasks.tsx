import { Link } from 'react-router-dom';
import type { Task } from '../api/types';
import { Revision, Timestamp } from '../components/common';
import { ExpandRow, NameLabel } from '../components/Row';
import { FilePill, StatusIcon, TaskIcon, TaskPill } from '../components/Status';
import type { IconName } from '../components/Icon';
import { TRIGGER, type Tone } from '../lib/status';
import { formatDuration } from '../lib/time';
import { useNow } from '../lib/useQuery';
import { ruleHref, taskHref } from './links';

export function shortId(taskId: string): string {
  return taskId.slice(0, 8);
}

export function taskSummary(t: Task): string {
  const error = t.files.find(f => f.error)?.error;
  if (error) return error;
  if (t.state === 'PENDING') return 'queued';
  if (t.state === 'RUNNING' || t.state === 'RETRYING') return 'pipeline running';
  const written = t.files.reduce((n, f) => n + f.written, 0);
  const pruned = t.files.reduce((n, f) => n + f.pruned, 0);
  if (t.dryRun) return `dry run · ${t.files.reduce((n, f) => n + (f.keys ?? 0), 0)} keys checked, nothing written`;
  return `${t.files.length} file${t.files.length === 1 ? '' : 's'} · wrote ${written} key${written === 1 ? '' : 's'}${pruned ? `, pruned ${pruned}` : ''}`;
}

type StageState = 'done' | 'active' | 'failed' | 'pending' | 'skipped';

const STAGE_LOOK: Record<StageState, { icon: IconName; tone: Tone; label: string }> = {
  done: { icon: 'checkCircle', tone: 'good', label: 'done' },
  active: { icon: 'loader', tone: 'progress', label: 'in progress' },
  failed: { icon: 'xCircle', tone: 'critical', label: 'failed' },
  pending: { icon: 'clock', tone: 'neutral', label: 'waiting' },
  skipped: { icon: 'minusCircle', tone: 'neutral', label: 'skipped' },
};

/** Pipeline stages: received → parse & validate → write (or simulate). */
export function taskStages(task: Task): { label: string; state: StageState }[] {
  const write = task.dryRun ? 'Simulate (no writes)' : 'Write to sink';
  const s = (a: StageState, b: StageState, c: StageState) => [
    { label: 'Received', state: a },
    { label: 'Parse & validate', state: b },
    { label: write, state: c },
  ];
  switch (task.state) {
    case 'PENDING':
      return s('active', 'pending', 'pending');
    case 'RUNNING':
    case 'RETRYING':
      return s('done', task.files.length ? 'done' : 'active', task.files.length ? 'active' : 'pending');
    case 'SUCCEEDED':
      return s('done', 'done', 'done');
    case 'FAILED': {
      const invalid = task.files.some(f => f.status === 'validation_failed');
      const writeFailed = task.files.some(f => f.status === 'error' || f.status === 'aborted');
      if (invalid || task.files.length === 0) return s('done', 'failed', 'skipped');
      return s('done', 'done', writeFailed ? 'failed' : 'done');
    }
  }
}

export function Stages({ task }: { task: Task }) {
  return (
    <div className="steps" aria-label="Pipeline stages">
      {taskStages(task).map(stage => {
        const look = STAGE_LOOK[stage.state];
        return (
          <span key={stage.label} className={`step step--${stage.state}`}>
            <StatusIcon icon={look.icon} tone={look.tone} spin={stage.state === 'active'} />
            {stage.label}
            <span className="sr-only">({look.label})</span>
          </span>
        );
      })}
    </div>
  );
}

export function TaskRow({ task: t, showTenant }: { task: Task; showTenant?: boolean }) {
  return (
    <ExpandRow
      label={`task ${shortId(t.taskId)}`}
      icon={<TaskIcon state={t.state} />}
      title={<NameLabel prefix={showTenant && t.tenant ? `${t.tenant} · ${t.rule ?? t.datasource}` : (t.rule ?? t.datasource)} name={t.env ?? shortId(t.taskId)} to={taskHref(t.taskId)} />}
      status={
        <>
          {t.dryRun && <span className="pill pill--purple">Dry run</span>}
          <TaskPill state={t.state} />
        </>
      }
      meta={
        <>
          {TRIGGER[t.trigger]} · {taskSummary(t)} · <Timestamp iso={t.createdAt} />
        </>
      }
      details={() => <TaskInline task={t} />}
    />
  );
}

export function TaskInline({ task: t }: { task: Task }) {
  const now = useNow(1000);
  return (
    <div className="stack" style={{ gap: 12 }}>
      <Stages task={t} />
      <dl className="facts">
        <div>
          <dt>Task</dt>
          <dd className="mono">{t.taskId}</dd>
        </div>
        <div>
          <dt>Initiated by</dt>
          <dd>
            {TRIGGER[t.trigger]}
            {t.submittedBy && <span className="muted"> · {t.submittedBy}</span>}
          </dd>
        </div>
        <div>
          <dt>Revision</dt>
          <dd>
            <Revision sha={t.revision} />
          </dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd className="num">{t.createdAt ? formatDuration(t.createdAt, t.finishedAt, now) : '—'}</dd>
        </div>
      </dl>
      {t.files.length > 0 && (
        <div className="table-wrap">
          <table className="table table--compact">
            <thead>
              <tr>
                <th>File</th>
                <th>Result</th>
                <th className="num">{t.dryRun ? 'Keys' : 'Written'}</th>
                <th className="num">Pruned</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {t.files.map((f, i) => (
                <tr key={`${f.file}:${i}`}>
                  <td className="mono">{f.file}</td>
                  <td>
                    <FilePill status={f.status} dryRun={t.dryRun} />
                  </td>
                  <td className="num">{t.dryRun ? f.keys ?? '—' : f.written}</td>
                  <td className="num">{f.pruned}</td>
                  <td style={{ color: f.error ? 'var(--red-text)' : undefined }}>{f.error ?? <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="action-bar">
        <Link className="abtn abtn--blue" to={taskHref(t.taskId)}>
          Open task
        </Link>
        {t.rule && (
          <Link className="abtn" to={ruleHref({ name: t.rule, tenant: t.tenant })}>
            Open rule
          </Link>
        )}
      </div>
    </div>
  );
}
