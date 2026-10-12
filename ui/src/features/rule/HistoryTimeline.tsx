import { Link } from 'react-router-dom';
import type { Task } from '../../api/types';
import { Revision } from '../../components/common';
import { TaskPill, TONE_COLOR } from '../../components/Status';
import { TASK } from '../../lib/status';
import { formatDateTime, formatDuration } from '../../lib/time';
import { useNow } from '../../lib/useQuery';
import { taskHref } from '../links';
import { shortId } from '../tasks';

export function initiatedBy(t: Task): string {
  switch (t.trigger) {
    case 'self_heal':
      return 'Automated · self-heal';
    case 'webhook':
      return `Git push${t.submittedBy ? ` by ${t.submittedBy}` : ''}`;
    case 'cli':
      return `CLI${t.submittedBy ? ` · ${t.submittedBy}` : ''}`;
    case 'ui':
      return `Manual${t.submittedBy ? ` · ${t.submittedBy}` : ''}`;
  }
}

/**
 * For each successful write, how long it stayed the live state of its
 * environment: until the next successful write there, or until now.
 */
export function activeSpans(tasks: Task[]): Map<string, { until?: string; current: boolean }> {
  const spans = new Map<string, { until?: string; current: boolean }>();
  const writes = tasks
    .filter(t => t.state === 'SUCCEEDED' && !t.dryRun && t.env)
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  const lastByEnv = new Map<string, Task>();
  for (const t of writes) {
    const prev = lastByEnv.get(t.env!);
    if (prev) spans.set(prev.taskId, { until: t.finishedAt ?? t.createdAt, current: false });
    lastByEnv.set(t.env!, t);
  }
  for (const t of lastByEnv.values()) spans.set(t.taskId, { current: true });
  return spans;
}

/** Vertical timeline of syncs, newest first. */
export function HistoryTimeline({ tasks, showEnv = true }: { tasks: Task[]; showEnv?: boolean }) {
  const now = useNow(30000);
  const spans = activeSpans(tasks);

  return (
    <ol className="timeline" aria-label="Sync history, newest first">
      {tasks.map(t => {
        const span = spans.get(t.taskId);
        const written = t.files.reduce((n, f) => n + f.written, 0);
        const pruned = t.files.reduce((n, f) => n + f.pruned, 0);
        const error = t.files.find(f => f.error)?.error;
        return (
          <li key={t.taskId} className="tl">
            <div className="tl__rail">
              <span className={`tl__dot bg-${TONE_COLOR[TASK[t.state].tone]}`} />
              <span className="tl__line" />
            </div>
            <div className="tl__body">
              <div className="row row--wrap" style={{ gap: 8 }}>
                <TaskPill state={t.state} />
                {showEnv && t.env && <span className="chip">{t.env}</span>}
                {t.dryRun && <span className="pill pill--purple">dry run</span>}
                {span?.current && <span className="pill pill--green">live now</span>}
              </div>
              <div className="tl__time">{formatDateTime(t.finishedAt ?? t.createdAt)}</div>
              <div className="tl__details">
                <span>
                  Initiated by: <b>{initiatedBy(t)}</b>
                </span>
                <span>
                  Duration: <b className="num">{t.finishedAt ? formatDuration(t.createdAt, t.finishedAt) : 'running'}</b>
                </span>
                {span && (
                  <span>
                    Active for: <b className="num">{formatDuration(t.finishedAt ?? t.createdAt, span.until, now)}</b>
                  </span>
                )}
                <span className="row" style={{ gap: 4 }}>
                  Revision: <Revision sha={t.revision} />
                </span>
                <span>
                  Task: <Link to={taskHref(t.taskId)} className="mono">{shortId(t.taskId)}</Link>
                </span>
              </div>
              <div style={{ fontSize: 13, color: error ? 'var(--red-text)' : 'var(--text-3)' }}>
                {error ??
                  (t.state === 'SUCCEEDED'
                    ? t.dryRun
                      ? 'Simulated — nothing written.'
                      : `Wrote ${written} key${written === 1 ? '' : 's'}${pruned ? `, pruned ${pruned}` : ''}.`
                    : t.message)}
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
