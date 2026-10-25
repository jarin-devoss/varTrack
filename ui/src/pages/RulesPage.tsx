import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useApi } from '../api';
import type { RuleView, TaskState } from '../api/types';
import { EmptyState, ErrorState, LoadingRows } from '../components/common';
import { FilterBar } from '../components/FilterBar';
import { TenantFilter } from '../components/TenantFilter';
import { Icon, SINK_LABEL } from '../components/Icon';
import { useShowMore } from '../components/Row';
import { SearchBox } from '../components/SearchBox';
import { StatusChart } from '../components/StatusChart';
import { NewRulePanel } from '../features/NewRulePanel';
import { RuleCard } from '../features/ruleViews';
import { SyncPanel } from '../features/SyncPanel';
import { usePageTitle, useSectionCount } from '../layout/page';
import { EMPTY_FACETS, facetsFromParams, filterRules, writeFacets, type FacetKey } from '../lib/filters';
import { usePinned } from '../lib/prefs';
import { isPinned, ruleKey, tenantsOf } from '../lib/rules';
import { DRIFT, DRIFT_SEVERITY, latestSync, ruleDrift, SYNC, TASK, targetSync, type SyncStatus } from '../lib/status';
import { useQuery } from '../lib/useQuery';
import { useRules } from '../lib/useRules';

type Sort = 'attention' | 'name' | 'recent';

const SORTS: Record<Sort, { label: string; compare: (a: RuleView, b: RuleView) => number }> = {
  attention: {
    label: 'Worst first',
    compare: (a, b) =>
      DRIFT_SEVERITY.indexOf(ruleDrift(a)) - DRIFT_SEVERITY.indexOf(ruleDrift(b)) ||
      failed(b) - failed(a) ||
      a.name.localeCompare(b.name),
  },
  name: { label: 'Name', compare: (a, b) => a.name.localeCompare(b.name) },
  recent: { label: 'Recently synced', compare: (a, b) => (latestSync(b.targets)?.startedAt ?? '').localeCompare(latestSync(a.targets)?.startedAt ?? '') },
};

function failed(r: RuleView) {
  return r.targets.filter(t => t.lastSync?.state === 'FAILED').length;
}

const SYNC_STATES: (TaskState | 'never')[] = ['FAILED', 'RETRYING', 'RUNNING', 'PENDING', 'SUCCEEDED', 'never'];
const STATUS_ORDER: SyncStatus[] = ['OutOfSync', 'Synced', 'Unknown'];

export function RulesPage() {
  usePageTitle('Rules');
  const api = useApi();
  const rules = useRules();
  const bundle = useQuery('bundle', api => api.getBundle(), { poll: false });
  const [params, setParams] = useSearchParams();
  const [pinned, togglePin] = usePinned();
  const [syncing, setSyncing] = useState<{ rule: RuleView; env?: string } | null>(null);
  const [creating, setCreating] = useState(false);

  const search = params.get('q') ?? '';
  const sort = (params.get('sort') as Sort) in SORTS ? (params.get('sort') as Sort) : 'attention';
  const facets = facetsFromParams(params);
  const all = rules.data ?? [];

  const set = (changes: { q?: string; sort?: Sort; facet?: [FacetKey, string] }) => {
    let p = new URLSearchParams(params);
    if (changes.q !== undefined) changes.q ? p.set('q', changes.q) : p.delete('q');
    if (changes.sort !== undefined) changes.sort === 'attention' ? p.delete('sort') : p.set('sort', changes.sort);
    if (changes.facet) p = writeFacets(p, { ...facets, [changes.facet[0]]: changes.facet[1] ? [changes.facet[1]] : [] });
    setParams(p, { replace: true });
  };
  const clear = () => setParams(writeFacets(new URLSearchParams(sort === 'attention' ? '' : `sort=${sort}`), EMPTY_FACETS), { replace: true });

  const facetKey = JSON.stringify(facets);
  const filtered = useMemo(() => filterRules(all, facets, search).sort(SORTS[sort].compare), [all, facetKey, search, sort]);
  const { visible, more } = useShowMore(filtered);
  useSectionCount('/rules', rules.data ? filtered.length : undefined, 'rules', rules.loading);

  const envs = [...new Set(all.flatMap(r => r.targets.map(t => t.env)))].sort();
  const sinks = [...new Set(all.map(r => r.sinkType))].sort();
  const tenants = tenantsOf(all);
  const targets = filtered.flatMap(r => r.targets);
  const active = Object.values(facets).some(v => v.length) || !!search;

  return (
    <div className="stack" style={{ gap: 12 }}>
      <FilterBar
        count={filtered.length}
        label="rules"
        loading={rules.loading}
        chart={
          <StatusChart
            unit="environments"
            onSelect={key => set({ facet: ['status', key] })}
            segments={STATUS_ORDER.map(s => ({ key: s, label: SYNC[s].label, tone: SYNC[s].tone, count: targets.filter(t => targetSync(t) === s).length }))}
          />
        }
      >
        <SearchBox value={search} onChange={q => set({ q })} placeholder="Search rules, repositories, files…" />
        <select className="ffield" aria-label="Sync status" value={facets.status[0] ?? ''} onChange={e => set({ facet: ['status', e.target.value] })}>
          <option value="">Any sync status</option>
          {STATUS_ORDER.map(s => <option key={s} value={s}>{SYNC[s].label}</option>)}
        </select>
        <select className="ffield" aria-label="Sink" value={facets.sink[0] ?? ''} onChange={e => set({ facet: ['sink', e.target.value] })}>
          <option value="">All sinks</option>
          {sinks.map(s => <option key={s} value={s}>{SINK_LABEL[s]}</option>)}
        </select>
        <select className="ffield" aria-label="Environment" value={facets.env[0] ?? ''} onChange={e => set({ facet: ['env', e.target.value] })}>
          <option value="">All environments</option>
          {envs.map(e => <option key={e}>{e}</option>)}
        </select>
        <TenantFilter tenants={tenants} value={facets.tenant[0] ?? ''} onChange={v => set({ facet: ['tenant', v] })} />
        <select className="ffield" aria-label="Last sync" value={facets.sync[0] ?? ''} onChange={e => set({ facet: ['sync', e.target.value] })}>
          <option value="">Any last sync</option>
          {SYNC_STATES.map(s => <option key={s} value={s}>{s === 'never' ? 'Never synced' : TASK[s].label}</option>)}
        </select>
        {facets.drift.length > 0 && (
          <button type="button" className="chip" onClick={() => set({ facet: ['drift', ''] })} title="Remove this filter">
            Self-heal: {facets.drift.map(d => DRIFT[d as keyof typeof DRIFT]?.label ?? d).join(', ')}
            <Icon name="x" size={12} />
          </button>
        )}
        <select className="ffield" aria-label="Sort" value={sort} onChange={e => set({ sort: e.target.value as Sort })}>
          {(Object.keys(SORTS) as Sort[]).map(s => <option key={s} value={s}>{SORTS[s].label}</option>)}
        </select>
        <button type="button" className="icon-btn icon-btn--plain" onClick={clear} disabled={!active} aria-label="Clear filters" title="Clear filters">
          <Icon name="x" size={16} />
        </button>
        <button type="button" className="icon-btn icon-btn--plain" onClick={rules.reload} aria-label="Refresh" title="Refresh">
          <Icon name="refresh" size={16} className={rules.fetching ? 'spin' : undefined} />
        </button>
        <button type="button" className="btn btn--primary btn--sm" onClick={() => setCreating(true)}>
          <Icon name="plus" size={14} /> New rule
        </button>
      </FilterBar>

      {rules.error && !rules.data ? (
        <div className="card">
          <ErrorState error={rules.error} what="rules" onRetry={rules.reload} />
        </div>
      ) : !rules.data ? (
        <LoadingRows rows={4} height={160} />
      ) : all.length === 0 && rules.hidden.length > 0 ? (
        <div className="card">
          <EmptyState
            icon="rules"
            title="No rules you can see yet"
            action={
              <span className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                {rules.hidden.map(h => (
                  <a key={h.platform} className="btn btn--primary btn--sm" href={api.loginUrl(h.platform, '/rules')}>
                    Connect {h.platform}
                  </a>
                ))}
              </span>
            }
          >
            {rules.hidden.map(h => `${h.rules} rule${h.rules === 1 ? '' : 's'} on ${h.platform}`).join(' and ')} {rules.hidden.reduce((n, h) => n + h.rules, 0) === 1 ? 'is' : 'are'} hidden
            because you are not connected there. What you see follows what Git lets your account read.
          </EmptyState>
        </div>
      ) : all.length === 0 ? (
        <div className="card">
          <EmptyState icon="rules" title="No rules in the bundle" action={<button className="btn btn--primary btn--sm" onClick={() => setCreating(true)}>New rule</button>}>
            Rules live in your CUE bundle (<code>config.cue</code>). Add one with a <code>platform</code>, <code>datasource</code>,{' '}
            <code>repositories</code> and <code>file_name</code>, push it, and it shows up here.
          </EmptyState>
        </div>
      ) : filtered.length === 0 ? (
        <div className="card">
          <EmptyState title="No rules match" action={<button className="btn btn--sm" onClick={() => { set({ q: '' }); clear(); }}>Clear search and filters</button>}>
            Nothing matches {search ? <>“{search}”</> : 'these filters'}.
          </EmptyState>
        </div>
      ) : (
        <>
          <div className="rcard-grid">
            {visible.map(r => (
              <RuleCard key={ruleKey(r)} rule={r} pinned={isPinned(pinned, r)} onTogglePin={() => togglePin(r)} onSync={() => setSyncing({ rule: r })} showTenant={tenants.length > 1} />
            ))}
          </div>
          {more}
        </>
      )}

      <SyncPanel
        open={!!syncing}
        rule={syncing?.rule ?? null}
        initialEnvs={syncing?.env ? [syncing.env] : undefined}
        onClose={() => setSyncing(null)}
        onStarted={rules.reload}
      />
      <NewRulePanel open={creating} onClose={() => setCreating(false)} bundle={bundle.data} existing={all.map(r => r.name)} />
    </div>
  );
}
