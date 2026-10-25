import { useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { EventReason } from '../api/types';
import { EmptyState, ErrorState, LoadingRows } from '../components/common';
import { FilterBar } from '../components/FilterBar';
import { Icon } from '../components/Icon';
import { useShowMore } from '../components/Row';
import { SearchBox } from '../components/SearchBox';
import { StatusChart } from '../components/StatusChart';
import { asGroups, EventRow, groupEvents, REASON } from '../features/events';
import { usePageTitle, useSectionCount } from '../layout/page';
import { useQuery } from '../lib/useQuery';
import { useTenants } from '../lib/useRules';
import { TenantFilter } from '../components/TenantFilter';

export function EventsPage() {
  usePageTitle('Events');
  const events = useQuery('events:all', api => api.listEvents({ limit: 500 }));
  const [params, setParams] = useSearchParams();

  const q = params.get('q') ?? '';
  const type = params.get('type') ?? '';
  const reason = (params.get('reason') ?? '') as EventReason | '';
  const rule = params.get('rule') ?? '';
  const tenant = params.get('tenant') ?? '';
  const tenants = useTenants();
  const grouped = params.get('group') !== 'off';
  const set = (key: string, value: string) => {
    const p = new URLSearchParams(params);
    value ? p.set(key, value) : p.delete(key);
    setParams(p, { replace: true });
  };

  const all = events.data ?? [];
  const rules = useMemo(() => [...new Set(all.map(e => e.rule).filter((r): r is string => !!r))].sort(), [all]);
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter(
      e =>
        (!type || e.type === type) &&
        (!reason || e.reason === reason) &&
        (!rule || e.rule === rule) &&
        (!tenant || e.tenant === tenant) &&
        (!needle || `${e.message} ${e.rule ?? ''} ${e.env ?? ''} ${REASON[e.reason].label}`.toLowerCase().includes(needle)),
    );
  }, [all, q, type, reason, rule, tenant]);
  const groups = useMemo(() => (grouped ? groupEvents(shown) : asGroups(shown)), [grouped, shown]);
  const { visible, more } = useShowMore(groups);
  useSectionCount('/events', events.data ? shown.length : undefined, 'events', events.loading);

  return (
    <div className="stack" style={{ gap: 12 }}>
      <FilterBar
        count={shown.length}
        label="events"
        loading={events.loading}
        chart={
          <StatusChart
            unit="events"
            onSelect={key => set('type', key)}
            segments={[
              { key: 'Normal', label: 'Normal', tone: 'good', count: shown.filter(e => e.type === 'Normal').length },
              { key: 'Warning', label: 'Warning', tone: 'critical', count: shown.filter(e => e.type === 'Warning').length },
            ]}
          />
        }
      >
        <SearchBox value={q} onChange={v => set('q', v)} placeholder="Search messages, rules, environments…" />
        <select className="ffield" aria-label="Type" value={type} onChange={e => set('type', e.target.value)}>
          <option value="">All types</option>
          <option>Normal</option>
          <option>Warning</option>
        </select>
        <select className="ffield" aria-label="Reason" value={reason} onChange={e => set('reason', e.target.value)}>
          <option value="">Any reason</option>
          {(Object.keys(REASON) as EventReason[]).map(r => <option key={r} value={r}>{REASON[r].label}</option>)}
        </select>
        <TenantFilter tenants={tenants} value={tenant} onChange={v => set('tenant', v)} />
        <select className="ffield" aria-label="Rule" value={rule} onChange={e => set('rule', e.target.value)}>
          <option value="">Any rule</option>
          {rules.map(r => <option key={r}>{r}</option>)}
        </select>
        <label className="check" style={{ padding: 0, fontSize: 12.5 }}>
          <input type="checkbox" checked={grouped} onChange={e => set('group', e.target.checked ? '' : 'off')} />
          <span>Group repeats</span>
        </label>
        <button type="button" className="icon-btn icon-btn--plain" onClick={() => setParams({}, { replace: true })} aria-label="Clear filters" title="Clear filters">
          <Icon name="x" size={16} />
        </button>
        <button type="button" className="icon-btn icon-btn--plain" onClick={events.reload} aria-label="Refresh" title="Refresh">
          <Icon name="refresh" size={16} className={events.fetching ? 'spin' : undefined} />
        </button>
      </FilterBar>

      {events.error && !events.data ? (
        <div className="card">
          <ErrorState error={events.error} what="events" onRetry={events.reload} />
        </div>
      ) : !events.data ? (
        <LoadingRows rows={8} height={30} />
      ) : groups.length === 0 ? (
        <div className="card">
          <EmptyState icon="activity" title={all.length ? 'No events match' : 'No events yet'}>
            {all.length ? 'Try a different filter.' : 'Events appear when a push arrives or the watcher detects drift.'}
          </EmptyState>
        </div>
      ) : (
        <div className="card card--flush rows">
          {visible.map(g => (
            <EventRow key={g.event.id} group={g} showTenant={tenants.length > 1} />
          ))}
          {more}
        </div>
      )}
    </div>
  );
}
