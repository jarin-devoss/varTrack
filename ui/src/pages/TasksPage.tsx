import { useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import type { TaskState, TaskTrigger } from '../api/types';
import { EmptyState, ErrorState, LoadingRows } from '../components/common';
import { FilterBar } from '../components/FilterBar';
import { Icon } from '../components/Icon';
import { useShowMore } from '../components/Row';
import { SearchBox } from '../components/SearchBox';
import { StatusChart } from '../components/StatusChart';
import { TaskRow } from '../features/tasks';
import { taskHref } from '../features/links';
import { usePageTitle, useSectionCount } from '../layout/page';
import { TASK, TRIGGER } from '../lib/status';
import { useQuery } from '../lib/useQuery';

const STATES: TaskState[] = ['FAILED', 'RETRYING', 'RUNNING', 'PENDING', 'SUCCEEDED'];

export function TasksPage() {
  usePageTitle('Tasks');
  const tasks = useQuery('tasks:all', api => api.listTasks({ limit: 300 }), { poll: 3000 });
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [lookup, setLookup] = useState('');

  const q = params.get('q') ?? '';
  const state = params.get('state') ?? '';
  const trigger = params.get('trigger') ?? '';
  const set = (key: string, value: string) => {
    const p = new URLSearchParams(params);
    value ? p.set(key, value) : p.delete(key);
    setParams(p, { replace: true });
  };

  const all = tasks.data ?? [];
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter(
      t =>
        (!state || t.state === state) &&
        (!trigger || t.trigger === trigger) &&
        (!needle || `${t.taskId} ${t.rule ?? ''} ${t.datasource} ${t.env ?? ''} ${t.submittedBy ?? ''}`.toLowerCase().includes(needle)),
    );
  }, [all, q, state, trigger]);
  const { visible, more } = useShowMore(shown);
  useSectionCount('/tasks', tasks.data ? shown.length : undefined, 'tasks', tasks.loading);

  const open = (e: React.FormEvent) => {
    e.preventDefault();
    if (lookup.trim()) navigate(taskHref(lookup.trim()));
  };

  return (
    <div className="stack" style={{ gap: 12 }}>
      <FilterBar
        count={shown.length}
        label="tasks"
        loading={tasks.loading}
        chart={
          <StatusChart
            unit="tasks"
            onSelect={key => set('state', key)}
            segments={STATES.map(s => ({ key: s, label: TASK[s].label, tone: TASK[s].tone, count: shown.filter(t => t.state === s).length }))}
          />
        }
      >
        <SearchBox value={q} onChange={v => set('q', v)} placeholder="Search task ID, rule, environment, user…" />
        <select className="ffield" aria-label="State" value={state} onChange={e => set('state', e.target.value)}>
          <option value="">All states</option>
          {STATES.map(s => <option key={s} value={s}>{TASK[s].label}</option>)}
        </select>
        <select className="ffield" aria-label="Trigger" value={trigger} onChange={e => set('trigger', e.target.value)}>
          <option value="">Any trigger</option>
          {(Object.keys(TRIGGER) as TaskTrigger[]).map(t => <option key={t} value={t}>{TRIGGER[t]}</option>)}
        </select>
        <button type="button" className="icon-btn icon-btn--plain" onClick={() => setParams({}, { replace: true })} aria-label="Clear filters" title="Clear filters">
          <Icon name="x" size={16} />
        </button>
        <form className="row" onSubmit={open} role="search" aria-label="Open a task by ID" style={{ gap: 6 }}>
          <input className="ffield mono" style={{ width: 230 }} placeholder="Open task by ID (from vt sync)" value={lookup} onChange={e => setLookup(e.target.value)} />
          <button className="btn btn--sm" type="submit" disabled={!lookup.trim()}>
            Open
          </button>
        </form>
      </FilterBar>

      {tasks.error && !tasks.data ? (
        <div className="card">
          <ErrorState error={tasks.error} what="the task list" onRetry={tasks.reload} />
          <p className="muted" style={{ textAlign: 'center', fontSize: 13 }}>You can still open a single task by ID above.</p>
        </div>
      ) : !tasks.data ? (
        <LoadingRows rows={8} height={30} />
      ) : shown.length === 0 ? (
        <div className="card">
          <EmptyState icon="tasks" title={all.length ? 'No tasks match' : 'No tasks yet'}>
            {all.length ? 'Try a different filter.' : 'Tasks appear when a push, CLI sync or self-heal runs the pipeline.'}
          </EmptyState>
        </div>
      ) : (
        <div className="card card--flush rows">
          {visible.map(t => (
            <TaskRow key={t.taskId} task={t} />
          ))}
          {more}
        </div>
      )}
    </div>
  );
}
