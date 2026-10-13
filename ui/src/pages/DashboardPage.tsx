import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { BundleView, RuleView, ServiceHealth, SinkType } from '../api/types';
import { EmptyState, ErrorState, LoadingRows, Timestamp } from '../components/common';
import { Hero, type HeroColor } from '../components/Hero';
import { Icon, SINK_ICON, SINK_LABEL, type IconName } from '../components/Icon';
import { Panel } from '../components/Panel';
import { EnvRow } from '../features/envViews';
import { EventRow, asGroups } from '../features/events';
import { SyncPanel } from '../features/SyncPanel';
import { usePageTitle } from '../layout/page';
import { needsAttention } from '../lib/status';
import { useQuery } from '../lib/useQuery';

interface Overall {
  color: HeroColor;
  icon: IconName;
  title: string;
  message: string;
  to?: string;
}

/** One headline for the whole tenant, worst first. */
export function overallStatus(rules: RuleView[]): Overall {
  const targets = rules.flatMap(r => r.targets);
  const failing = targets.filter(t => t.drift === 'heal_failed' || t.lastSync?.state === 'FAILED').length;
  const drifting = targets.filter(t => t.drift === 'drifted' || t.drift === 'unknown').length;
  const busy = targets.filter(t => t.drift === 'healing' || t.lastSync?.state === 'RUNNING' || t.lastSync?.state === 'PENDING').length;
  const plural = (n: number) => `${n} environment${n === 1 ? '' : 's'}`;

  if (targets.length === 0) return { color: 'gray', icon: 'rules', title: 'No rules yet', message: 'Add a rule to config.cue and push it.' };
  if (failing > 0)
    return { color: 'red', icon: 'xCircle', title: 'Action Needed', message: `${plural(failing)} failing${drifting ? `, ${drifting} drifting` : ''}`, to: '/rules?drift=heal_failed' };
  if (drifting > 0) return { color: 'yellow', icon: 'alertTriangle', title: 'Out of Sync', message: `${plural(drifting)} differ from Git`, to: '/rules?status=OutOfSync' };
  if (busy > 0) return { color: 'blue', icon: 'loader', title: 'Syncing', message: `${plural(busy)} being written from Git` };
  return { color: 'green', icon: 'check', title: 'All Environments In Sync', message: 'Every watched environment matches Git' };
}

export function DashboardPage() {
  usePageTitle('Home');
  const rules = useQuery('rules', api => api.listRules());
  const bundle = useQuery('bundle', api => api.getBundle(), { poll: false });
  const health = useQuery('health', api => api.getHealth(), { poll: 15000 });
  const events = useQuery('events:recent', api => api.listEvents({ limit: 8 }));
  const [syncing, setSyncing] = useState<{ rule: RuleView; env: string } | null>(null);

  if (rules.error && !rules.data) {
    return (
      <div className="card">
        <ErrorState error={rules.error} what="rules" onRetry={rules.reload} />
      </div>
    );
  }
  if (!rules.data) return <LoadingRows rows={4} height={110} />;

  const overall = overallStatus(rules.data);
  const targets = rules.data.flatMap(r => r.targets);

  return (
    <div className="page-stack">
      <Hero color={overall.color} icon={overall.icon} spin={overall.icon === 'loader'} to={overall.to} lastUpdated={rules.updatedAt ? new Date(rules.updatedAt).toISOString() : undefined}>
        <div className="hero__title">{overall.title}</div>
        <div className="hero__meta">{overall.message}</div>
        <div className="muted" style={{ fontSize: 13, marginTop: 2 }}>
          {rules.data.length} rules · {targets.length} environments · {new Set(rules.data.map(r => r.sinkType)).size} sink types
        </div>
      </Hero>

      <AttentionPanel rules={rules.data} onSync={(rule, env) => setSyncing({ rule, env })} />

      <Panel title="Tenant" subtitle="Where the bundle comes from and what it connects to">
        <TenantFacts bundle={bundle.data} rules={rules.data} error={bundle.error} />
      </Panel>

      <Panel title="Services" subtitle="gateway, orchestrator and watcher readiness">
        {health.data ? <ServiceRows services={health.data} /> : <LoadingRows rows={3} height={36} />}
      </Panel>

      <Panel title="Sinks" subtitle="Environments per sink type and how they are doing">
        <SinkTable rules={rules.data} />
      </Panel>

      <Panel
        title="Recent events"
        subtitle={
          <>
            Newest first · <Link to="/events">all events</Link>
          </>
        }
      >
        {events.data ? (
          events.data.length ? (
            <div>{asGroups(events.data).map(g => <EventRow key={g.event.id} group={g} />)}</div>
          ) : (
            <EmptyState compact icon="activity" title="No events yet" />
          )
        ) : events.error ? (
          <ErrorState compact error={events.error} what="events" onRetry={events.reload} />
        ) : (
          <LoadingRows rows={4} height={30} />
        )}
      </Panel>

      <SyncPanel open={!!syncing} rule={syncing?.rule ?? null} initialEnvs={syncing ? [syncing.env] : undefined} onClose={() => setSyncing(null)} onStarted={rules.reload} />
    </div>
  );
}

function AttentionPanel({ rules, onSync }: { rules: RuleView[]; onSync: (rule: RuleView, env: string) => void }) {
  const attention = useMemo(() => needsAttention(rules), [rules]);
  const seen = new Set<string>();
  const items = attention.filter(a => {
    const key = `${a.rule.name}|${a.target.env}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return (
    <Panel title="Needs attention" subtitle={items.length ? `${items.length} environment${items.length === 1 ? '' : 's'} with drift, an unknown state or a failed sync` : 'Nothing to do'}>
      {items.length === 0 ? (
        <EmptyState compact icon="checkCircle" title="Everything is in sync">
          Every watched environment matches Git and every last sync succeeded.
        </EmptyState>
      ) : (
        <div>
          {items.map(a => (
            <EnvRow key={`${a.rule.name}|${a.target.env}`} rule={a.rule} target={a.target} onSync={() => onSync(a.rule, a.target.env)} />
          ))}
        </div>
      )}
    </Panel>
  );
}

function TenantFacts({ bundle, rules, error }: { bundle?: BundleView; rules: RuleView[]; error?: unknown }) {
  if (!bundle) {
    return error ? <ErrorState compact error={error} what="the bundle" /> : <LoadingRows rows={2} height={40} />;
  }
  return (
    <dl className="facts">
      <div>
        <dt>Tenant</dt>
        <dd className="mono">{bundle.tenant}</dd>
      </div>
      <div>
        <dt>Platforms</dt>
        <dd>{bundle.platforms.map(p => `${p.name} (${p.endpoint})`).join(', ')}</dd>
      </div>
      <div>
        <dt>Datasources</dt>
        <dd>
          <Link to="/settings">{bundle.datasources.length} configured</Link>
        </dd>
      </div>
      <div>
        <dt>Rules</dt>
        <dd>
          {rules.length} · {rules.filter(r => r.selfHeal).length} with self-heal
        </dd>
      </div>
      <div>
        <dt>Secret managers</dt>
        <dd>{bundle.secretManagers.map(s => `${s.name} — ${s.type}`).join(', ') || <span className="muted">none</span>}</dd>
      </div>
      <div>
        <dt>Schema registry</dt>
        <dd className="mono">
          {bundle.schemaRegistry ? `${bundle.schemaRegistry.repo}@${bundle.schemaRegistry.branch}` : <span className="muted">none</span>}
        </dd>
      </div>
    </dl>
  );
}

const SERVICE_ROLE: Record<ServiceHealth['name'], string> = {
  gateway: 'Receives webhooks and CLI calls',
  orchestrator: 'Runs the ETL pipeline and writes sinks',
  watcher: 'Polls sinks for drift, triggers self-heal',
};

export function ServiceRows({ services }: { services: ServiceHealth[] }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Service</th>
            <th>Status</th>
            <th>Checks</th>
            <th>Checked</th>
          </tr>
        </thead>
        <tbody>
          {services.map(s => (
            <tr key={s.name}>
              <td>
                <div style={{ fontWeight: 600, textTransform: 'capitalize' }}>{s.name}</div>
                <div className="muted" style={{ fontSize: 12 }}>{s.error ?? SERVICE_ROLE[s.name]}</div>
              </td>
              <td>
                <span className={`pill pill--${s.status === 'up' ? 'green' : s.status === 'down' ? 'red' : 'yellow'}`}>
                  {s.status === 'up' ? 'Ready' : s.status === 'down' ? 'Unreachable' : 'Not ready'}
                </span>
              </td>
              <td>
                <div className="row row--wrap" style={{ gap: 4 }}>
                  {Object.entries(s.checks).map(([k, v]) => (
                    <span key={k} className="tag">
                      {k.replace(/_/g, ' ')}: {v}
                    </span>
                  ))}
                  {Object.keys(s.checks).length === 0 && <span className="muted">—</span>}
                </div>
              </td>
              <td className="muted" style={{ whiteSpace: 'nowrap' }}>
                <Timestamp iso={s.checkedAt} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SinkTable({ rules }: { rules: RuleView[] }) {
  const rows = useMemo(() => {
    const bySink = new Map<SinkType, RuleView[]>();
    for (const r of rules) bySink.set(r.sinkType, [...(bySink.get(r.sinkType) ?? []), r]);
    return [...bySink.entries()].map(([sink, rs]) => {
      const ts = rs.flatMap(r => r.targets);
      return {
        sink,
        rules: rs.length,
        envs: ts.length,
        inSync: ts.filter(t => t.drift === 'in_sync').length,
        drifting: ts.filter(t => t.drift === 'drifted' || t.drift === 'healing' || t.drift === 'heal_failed').length,
        failing: ts.filter(t => t.drift === 'heal_failed' || t.lastSync?.state === 'FAILED').length,
        unwatched: ts.filter(t => t.drift === 'not_watched').length,
        keys: ts.reduce((n, t) => n + t.keyCount, 0),
      };
    });
  }, [rules]);

  const count = (n: number, color: string) => (n ? <span className={`pill pill--${color}`}>{n}</span> : <span className="faint">0</span>);

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Sink</th>
            <th className="num">Rules</th>
            <th className="num">Environments</th>
            <th className="num">In sync</th>
            <th className="num">Drifting</th>
            <th className="num">Failing</th>
            <th className="num">Not watched</th>
            <th className="num">Keys</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.sink}>
              <td>
                <Link to={`/rules?sink=${r.sink}`} className="row" style={{ gap: 8 }}>
                  <Icon name={SINK_ICON[r.sink]} size={15} className="muted" />
                  {SINK_LABEL[r.sink]}
                </Link>
              </td>
              <td className="num">{r.rules}</td>
              <td className="num">{r.envs}</td>
              <td className="num">{count(r.inSync, 'green')}</td>
              <td className="num">{count(r.drifting, 'yellow')}</td>
              <td className="num">{count(r.failing, 'red')}</td>
              <td className="num">{count(r.unwatched, 'gray')}</td>
              <td className="num">{r.keys}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
