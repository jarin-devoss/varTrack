import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { isNotAvailable } from '../api/http';
import type { TaskState } from '../api/types';
import { CopyButton, EmptyState, ErrorState, LoadingRows, Notice, Revision, Timestamp } from '../components/common';
import { Hero, type HeroColor } from '../components/Hero';
import { Panel } from '../components/Panel';
import { FilePill, taskIcon } from '../components/Status';
import { envHref, ruleHref } from '../features/links';
import { initiatedBy } from '../features/rule/HistoryTimeline';
import { shortId, Stages, taskSummary } from '../features/tasks';
import { usePageTitle } from '../layout/page';
import { isActive, TRIGGER } from '../lib/status';
import { formatDateTime, formatDuration } from '../lib/time';
import { useNow, useQuery } from '../lib/useQuery';

const COLOR: Record<TaskState, HeroColor> = { SUCCEEDED: 'green', FAILED: 'red', RUNNING: 'blue', PENDING: 'yellow', RETRYING: 'yellow' };

export function TaskPage() {
  const { taskId = '' } = useParams();
  usePageTitle(`Task ${shortId(taskId)}`);
  const [finished, setFinished] = useState(false);
  const task = useQuery(`task:${taskId}`, api => api.getTask(taskId), { poll: finished ? false : 1500 });
  const now = useNow(1000);

  useEffect(() => {
    if (task.data) setFinished(!isActive(task.data.state));
  }, [task.data?.state]);

  if (task.error && !task.data) {
    const notFound = !isNotAvailable(task.error) && (task.error as { status?: number }).status === 404;
    return (
      <div className="card">
        {notFound ? (
          <EmptyState icon="tasks" title="Task not found" action={<Link className="btn btn--sm" to="/tasks">All tasks</Link>}>
            No task with ID <code>{taskId}</code>. Results expire from the result backend after a while.
          </EmptyState>
        ) : (
          <ErrorState error={task.error} what="this task" onRetry={task.reload} />
        )}
      </div>
    );
  }
  if (!task.data) return <LoadingRows rows={3} height={110} />;

  const t = task.data;
  const checked = t.files.reduce((n, f) => n + (f.keys ?? 0), 0);
  // Dry runs recorded before the orchestrator said so read "0 keys written".
  const result = t.dryRun && t.state === 'SUCCEEDED' ? `${checked} key${checked === 1 ? '' : 's'} checked, nothing written` : t.message;
  return (
    <div className="page-stack">
      <Hero
        color={COLOR[t.state]}
        icon={t.state === 'SUCCEEDED' ? 'check' : taskIcon(t.state)}
        spin={t.state === 'RUNNING'}
        kind={`Task · ${TRIGGER[t.trigger]}${t.dryRun ? ' · dry run' : ''}`}
        name={<span className="mono">{shortId(t.taskId)}</span>}
        titleAction={<CopyButton text={t.taskId} label="Copy task ID" />}
        meta={
          t.rule ? (
            <>
              <Link to={ruleHref(t.rule)}>{t.rule}</Link>
              {t.env && (
                <>
                  {' · '}
                  <Link to={envHref(t.rule, t.env)}>{t.env}</Link>
                </>
              )}
            </>
          ) : (
            <span className="mono">{t.datasource || 'unknown datasource'}</span>
          )
        }
        lastUpdated={t.finishedAt}
      />

      {t.partial && (
        <Notice tone="info">
          The orchestrator reports only the task state and its result for this lookup, so the rule, trigger and timing may be incomplete.
        </Notice>
      )}

      <Panel title="Pipeline" subtitle={taskSummary(t)}>
        <div className="stack" style={{ gap: 20 }}>
          <Stages task={t} />
          <dl className="facts">
            <div>
              <dt>Task ID</dt>
              <dd className="mono">{t.taskId}</dd>
            </div>
            <div>
              <dt>Initiated by</dt>
              <dd>{initiatedBy(t)}</dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd className="mono">{t.repository ? `${t.repository}@${t.ref ?? ''}` : t.ref ?? '—'}</dd>
            </div>
            <div>
              <dt>Revision</dt>
              <dd>
                <Revision sha={t.revision} />
              </dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd>
                {t.createdAt ? (
                  <>
                    <Timestamp iso={t.createdAt} /> <span className="muted">· {formatDateTime(t.createdAt)}</span>
                  </>
                ) : (
                  '—'
                )}
              </dd>
            </div>
            <div>
              <dt>Duration</dt>
              <dd className="num">
                {t.createdAt ? formatDuration(t.createdAt, t.finishedAt, now) : '—'}
                {!t.finishedAt && isActive(t.state) && ' (running)'}
              </dd>
            </div>
            {result && (
              <div>
                <dt>Result</dt>
                <dd className="mono">{result}</dd>
              </div>
            )}
          </dl>
        </div>
      </Panel>

      <Panel title="Files" subtitle={`${t.files.length} file${t.files.length === 1 ? '' : 's'}`}>
        {t.files.length === 0 ? (
          <EmptyState compact icon={isActive(t.state) ? 'loader' : 'file'} title={isActive(t.state) ? 'Waiting for results' : 'No file results'}>
            {isActive(t.state) ? 'Per-file results appear when the pipeline finishes.' : t.message}
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Environment</th>
                  <th>Result</th>
                  <th>Sync mode</th>
                  <th className="num">{t.dryRun ? 'Keys' : 'Written'}</th>
                  <th className="num">Pruned</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {t.files.map((f, i) => (
                  <tr key={`${f.file}:${i}`}>
                    <td className="mono">{f.file}</td>
                    <td>{f.env}</td>
                    <td>
                      <FilePill status={f.status} dryRun={t.dryRun} />
                    </td>
                    <td className="mono muted">{f.syncMode || '—'}</td>
                    <td className="num">{t.dryRun ? f.keys ?? '—' : f.written}</td>
                    <td className="num">{f.pruned}</td>
                    <td style={{ minWidth: 240 }}>
                      {f.error ? (
                        <span className="value" style={{ color: 'var(--red-text)' }}>
                          {f.error}
                        </span>
                      ) : f.sample ? (
                        <details>
                          <summary className="muted" style={{ cursor: 'pointer' }}>
                            Sample values
                          </summary>
                          <pre className="code" style={{ marginTop: 6 }}>
                            {Object.entries(f.sample)
                              .map(([k, v]) => `${k} = ${v}`)
                              .join('\n')}
                          </pre>
                        </details>
                      ) : (
                        <span className="muted">{f.validation ? `validation: ${f.validation}` : '—'}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
