import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { RuleView, Target } from '../api/types';
import { EmptyState, ErrorState, LoadingRows } from '../components/common';
import { FilterBar } from '../components/FilterBar';
import { Icon, SINK_LABEL } from '../components/Icon';
import { useShowMore } from '../components/Row';
import { SearchBox } from '../components/SearchBox';
import { StatusChart } from '../components/StatusChart';
import { EnvRow } from '../features/envViews';
import { SyncPanel } from '../features/SyncPanel';
import { usePageTitle, useSectionCount } from '../layout/page';
import { DRIFT_SEVERITY, SYNC, targetSync, type SyncStatus } from '../lib/status';
import { tenantsOf } from '../lib/rules';
import { useRules } from '../lib/useRules';
import { TenantFilter } from '../components/TenantFilter';

const STATUS_ORDER: SyncStatus[] = ['OutOfSync', 'Synced', 'Unknown'];
const SYNC_RANK: Record<SyncStatus, number> = { OutOfSync: 0, Unknown: 1, Synced: 2 };

/** Every environment of every rule, one row each. */
export function EnvironmentsPage() {
  usePageTitle('Environments');
  const rules = useRules();
  const [params, setParams] = useSearchParams();
  const [syncing, setSyncing] = useState<{ rule: RuleView; env: string } | null>(null);

  const q = params.get('q') ?? '';
  const status = params.get('status') ?? '';
  const sink = params.get('sink') ?? '';
  const env = params.get('env') ?? '';
  const tenant = params.get('tenant') ?? '';
  const set = (key: string, value: string) => {
    const p = new URLSearchParams(params);
    value ? p.set(key, value) : p.delete(key);
    setParams(p, { replace: true });
  };

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const all: { rule: RuleView; target: Target }[] = (rules.data ?? []).flatMap(rule => rule.targets.map(target => ({ rule, target })));
    return all
      .filter(
        ({ rule, target }) =>
          (!status || targetSync(target) === status) &&
          (!sink || rule.sinkType === sink) &&
          (!env || target.env === env) &&
          (!tenant || rule.tenant === tenant) &&
          (!needle || `${rule.name} ${target.env} ${target.destination} ${target.repository}`.toLowerCase().includes(needle)),
      )
      .sort(
        (a, b) =>
          SYNC_RANK[targetSync(a.target)] - SYNC_RANK[targetSync(b.target)] ||
          DRIFT_SEVERITY.indexOf(a.target.drift) - DRIFT_SEVERITY.indexOf(b.target.drift) ||
          a.rule.name.localeCompare(b.rule.name),
      );
  }, [rules.data, q, status, sink, env, tenant]);
  const { visible, more } = useShowMore(rows);
  useSectionCount('/environments', rules.data ? rows.length : undefined, 'environments', rules.loading);

  const all = rules.data ?? [];
  const envs = [...new Set(all.flatMap(r => r.targets.map(t => t.env)))].sort();
  const sinks = [...new Set(all.map(r => r.sinkType))].sort();
  const tenants = tenantsOf(all);

  return (
    <div className="stack" style={{ gap: 12 }}>
      <FilterBar
        count={rows.length}
        label="environments"
        loading={rules.loading}
        chart={
          <StatusChart
            unit="environments"
            onSelect={key => set('status', key)}
            segments={STATUS_ORDER.map(s => ({ key: s, label: SYNC[s].label, tone: SYNC[s].tone, count: rows.filter(r => targetSync(r.target) === s).length }))}
          />
        }
      >
        <SearchBox value={q} onChange={v => set('q', v)} placeholder="Search environments, destinations, repositories…" />
        <select className="ffield" aria-label="Sync status" value={status} onChange={e => set('status', e.target.value)}>
          <option value="">Any sync status</option>
          {STATUS_ORDER.map(s => <option key={s} value={s}>{SYNC[s].label}</option>)}
        </select>
        <select className="ffield" aria-label="Sink" value={sink} onChange={e => set('sink', e.target.value)}>
          <option value="">All sinks</option>
          {sinks.map(s => <option key={s} value={s}>{SINK_LABEL[s]}</option>)}
        </select>
        <select className="ffield" aria-label="Environment" value={env} onChange={e => set('env', e.target.value)}>
          <option value="">All environments</option>
          {envs.map(e => <option key={e}>{e}</option>)}
        </select>
        <TenantFilter tenants={tenants} value={tenant} onChange={v => set('tenant', v)} />
        <button type="button" className="icon-btn icon-btn--plain" onClick={() => setParams({}, { replace: true })} aria-label="Clear filters" title="Clear filters">
          <Icon name="x" size={16} />
        </button>
        <button type="button" className="icon-btn icon-btn--plain" onClick={rules.reload} aria-label="Refresh" title="Refresh">
          <Icon name="refresh" size={16} className={rules.fetching ? 'spin' : undefined} />
        </button>
      </FilterBar>

      {rules.error && !rules.data ? (
        <div className="card">
          <ErrorState error={rules.error} what="environments" onRetry={rules.reload} />
        </div>
      ) : !rules.data ? (
        <LoadingRows rows={8} height={34} />
      ) : rows.length === 0 ? (
        <div className="card">
          <EmptyState title="No environments match">Try a different filter.</EmptyState>
        </div>
      ) : (
        <div className="card card--flush rows">
          {visible.map(({ rule, target }) => (
            <EnvRow key={`${rule.tenant}|${rule.name}|${target.env}`} rule={rule} target={target} showTenant={tenants.length > 1} onSync={() => setSyncing({ rule, env: target.env })} />
          ))}
          {more}
        </div>
      )}

      <SyncPanel open={!!syncing} rule={syncing?.rule ?? null} initialEnvs={syncing ? [syncing.env] : undefined} onClose={() => setSyncing(null)} onStarted={rules.reload} />
    </div>
  );
}
