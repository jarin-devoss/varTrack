import { useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { useApi } from '../api';
import { isNotAvailable } from '../api/http';
import type { RuleView } from '../api/types';
import { EmptyState, ErrorState, LoadingRows, Notice, Segmented, Timestamp } from '../components/common';
import { Hero, type HeroColor } from '../components/Hero';
import { Icon, SINK_LABEL } from '../components/Icon';
import { Panel, UTabs } from '../components/Panel';
import { Star } from '../components/Row';
import { DriftPill, driftIcon, OpPill, SyncPill } from '../components/Status';
import { asGroups, EventRow } from '../features/events';
import { taskHref } from '../features/links';
import { DefinitionTab } from '../features/rule/DefinitionTab';
import { DiffOptions, EnvDiff } from '../features/rule/DiffView';
import { HistoryTimeline } from '../features/rule/HistoryTimeline';
import { KeysTab, type KeyFilter } from '../features/rule/KeysTab';
import { ResolutionSteps } from '../features/rule/resolution';
import { fileSummary, repoSummary, ruleColor } from '../features/ruleViews';
import { EnvBox, EnvPanel, ENV_VIEWS, type EnvView } from '../features/state';
import { SyncPanel } from '../features/SyncPanel';
import { usePageTitle } from '../layout/page';
import { usePinned } from '../lib/prefs';
import { DRIFT_SEVERITY, ruleDrift, ruleSync, TRIGGER, targetSync, type SyncStatus } from '../lib/status';
import { peekQuery, useQuery } from '../lib/useQuery';

type RuleTab = 'overview' | 'history' | 'events' | 'definition';
type EnvTab = 'environments' | 'keys' | 'diff';
const ENV_TABS: EnvTab[] = ['environments', 'keys', 'diff'];
const KEY_FILTERS: KeyFilter[] = ['all', 'changed', 'secrets', 'protected'];
const SYNC_RANK: Record<SyncStatus, number> = { OutOfSync: 0, Unknown: 1, Synced: 2 };

export function RulePage() {
  const { platform = '', datasource = '' } = useParams();
  const name = `${platform}/${datasource}`;
  usePageTitle(name);
  const api = useApi();
  // Opened from a list: show the rule the list already has while it loads.
  const rule = useQuery(`rule:${name}`, a => a.getRule(name), {
    placeholder: () => peekQuery<RuleView[]>(api, 'rules')?.find(r => r.name === name),
  });
  const [params, setParams] = useSearchParams();
  const [pinned, togglePin] = usePinned();
  const [sync, setSync] = useState<{ envs?: string[]; dryRun: boolean } | null>(null);
  const [ruleTab, setRuleTab] = useState<RuleTab>('overview');
  const [keysEnv, setKeysEnv] = useState<string>();

  const setParam = (changes: Record<string, string | null>) => {
    const p = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) v === null ? p.delete(k) : p.set(k, v);
    setParams(p, { replace: true });
  };

  if (rule.error && !rule.data) {
    const missing = !isNotAvailable(rule.error) && (rule.error as { status?: number }).status === 404;
    return (
      <div className="card">
        {missing ? (
          <EmptyState icon="rules" title={`No rule named ${name}`} action={<Link className="btn btn--sm" to="/rules">All rules</Link>}>
            It may have been removed from the bundle.
          </EmptyState>
        ) : (
          <ErrorState error={rule.error} what="this rule" onRetry={rule.reload} />
        )}
      </div>
    );
  }
  if (!rule.data) return <LoadingRows rows={4} height={110} />;

  const r = rule.data;
  const envTab = ENV_TABS.includes(params.get('tab') as EnvTab) ? (params.get('tab') as EnvTab) : 'environments';
  const openEnv = r.targets.some(t => t.env === params.get('env')) ? params.get('env')! : undefined;
  const envView = ENV_VIEWS.includes(params.get('view') as EnvView) ? (params.get('view') as EnvView) : 'summary';
  const keyFilter = KEY_FILTERS.includes(params.get('keys') as KeyFilter) ? (params.get('keys') as KeyFilter) : 'all';
  const color = ruleColor(r) as HeroColor;
  const openSync = (envs?: string[], dryRun = false) => setSync({ envs, dryRun });
  const sorted = [...r.targets].sort(
    (a, b) => SYNC_RANK[targetSync(a)] - SYNC_RANK[targetSync(b)] || DRIFT_SEVERITY.indexOf(a.drift) - DRIFT_SEVERITY.indexOf(b.drift),
  );

  return (
    <div className="page-stack">
      <Hero
        color={color}
        icon={color === 'green' ? 'check' : driftIcon(ruleDrift(r))}
        spin={ruleDrift(r) === 'healing'}
        kind={`Rule · ${SINK_LABEL[r.sinkType]}`}
        name={r.name}
        titleAction={<Star active={pinned.includes(r.name)} onToggle={() => togglePin(r.name)} label={r.name} />}
        meta={
          <>
            Tenant: {r.tenant} · {repoSummary(r)}
          </>
        }
        lastUpdated={rule.updatedAt ? new Date(rule.updatedAt).toISOString() : undefined}
      />

      <div className="action-bar">
        <button className="abtn abtn--blue" onClick={() => openSync()}>
          <Icon name="refresh" size={14} /> Sync
        </button>
        <button className="abtn abtn--purple" onClick={() => openSync(undefined, true)}>
          <Icon name="play" size={14} /> Dry run
        </button>
        <button className="abtn" onClick={() => setParam({ tab: 'diff', env: null, view: null })}>
          <Icon name="diff" size={14} /> Diff
        </button>
        <button className="abtn" onClick={rule.reload} title="Fetch the latest state">
          <Icon name="refresh" size={14} className={rule.fetching ? 'spin' : undefined} /> Refresh
        </button>
      </div>

      <RuleStatus rule={r} onRetry={envs => openSync(envs)} />

      {r.dryRun && (
        <Notice tone="info">
          This rule has <code>dry_run: true</code>: every sync runs the full pipeline but writes nothing.
        </Notice>
      )}

      <Panel title="Environments" subtitle={<>Desired state in Git compared with the current state in {SINK_LABEL[r.sinkType]}</>} id="environments-panel">
        <UTabs
          label="Environments"
          active={envTab}
          onChange={t => setParam({ tab: t === 'environments' ? null : t })}
          tabs={[
            { id: 'environments', label: 'Environments', count: r.targets.length },
            { id: 'keys', label: 'Keys' },
            { id: 'diff', label: 'Diff' },
          ]}
        />
        {envTab === 'environments' && (
          <div className="envbox-list">
            {sorted.map(t => (
              <EnvBox
                key={t.env}
                rule={r}
                target={t}
                onOpen={view => setParam({ env: t.env, view: view && view !== 'summary' ? view : null })}
                onSync={() => openSync([t.env])}
              />
            ))}
          </div>
        )}
        {envTab === 'keys' && (
          <KeysTab
            rule={r}
            env={keysEnv ?? sorted[0]?.env}
            onEnvChange={setKeysEnv}
            filter={keyFilter}
            onFilterChange={f => setParam({ keys: f === 'all' ? null : f })}
            onSync={e => openSync([e])}
          />
        )}
        {envTab === 'diff' && <RuleDiff rule={r} />}
      </Panel>

      <Panel title="Rule" subtitle="How the rule is configured and what it has done" id="rule-panel">
        <UTabs
          label="Rule"
          active={ruleTab}
          onChange={setRuleTab}
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'history', label: 'History' },
            { id: 'events', label: 'Events' },
            { id: 'definition', label: 'Definition' },
          ]}
        />
        {ruleTab === 'overview' && <RuleOverview rule={r} />}
        {ruleTab === 'history' && <RuleHistory rule={r} />}
        {ruleTab === 'events' && <RuleEvents rule={r} />}
        {ruleTab === 'definition' && <DefinitionTab rule={r} />}
      </Panel>

      <EnvPanel
        rule={r}
        env={openEnv}
        view={envView}
        onView={v => setParam({ view: v === 'summary' ? null : v })}
        onClose={() => setParam({ env: null, view: null })}
        onSync={e => openSync([e])}
      />
      <SyncPanel open={!!sync} rule={r} initialEnvs={sync?.envs} initialDryRun={sync?.dryRun} onClose={() => setSync(null)} onStarted={rule.reload} />
    </div>
  );
}

function healText(r: RuleView): React.ReactNode {
  const envs = (d: string) => r.targets.filter(t => t.drift === d).map(t => t.env).join(', ');
  if (!r.selfHeal) return "The watcher doesn't compare this rule with Git. Sync to restore Git values.";
  switch (ruleDrift(r)) {
    case 'heal_failed':
      return `Gave up on ${envs('heal_failed')}. ${r.targets.find(t => t.drift === 'heal_failed')?.driftMessage ?? ''}`;
    case 'drifted':
      return `Restoring ${envs('drifted')} from Git shortly.`;
    case 'healing':
      return `Writing Git values back to ${envs('healing')}.`;
    case 'unknown':
      return `Can't read ${envs('unknown')}.`;
    default:
      return 'Changes made outside Git are put back automatically.';
  }
}

/** Three boxes: is the sink in sync with Git, is self-heal keeping it there, and how the last sync went. */
function RuleStatus({ rule: r, onRetry }: { rule: RuleView; onRetry: (envs: string[]) => void }) {
  const out = r.targets.filter(t => targetSync(t) === 'OutOfSync');
  const synced = r.targets.filter(t => targetSync(t) === 'Synced').length;
  const drift = ruleDrift(r);
  const failedHeal = r.targets.filter(t => t.drift === 'heal_failed');
  const last = r.targets.filter(t => t.lastSync).sort((a, b) => b.lastSync!.startedAt.localeCompare(a.lastSync!.startedAt))[0];
  const op = last?.lastSync;

  return (
    <div className="statgrid">
      <section className="card stat">
        <div className="stat__label">Sync status</div>
        <div className="stat__value">
          <SyncPill status={ruleSync(r)} large />
        </div>
        <div className="stat__sub">
          {synced} of {r.targets.length} environment{r.targets.length === 1 ? '' : 's'} match Git
          {out.length > 0 && (
            <>
              {' '}
              · {out.map(t => t.env).join(', ')} differ{out.length === 1 ? 's' : ''}
            </>
          )}
        </div>
      </section>
      <section className="card stat">
        <div className="stat__label">Self-heal</div>
        <div className="stat__value">
          {!r.selfHeal ? (
            <span className="pill pill--gray pill--lg">Off</span>
          ) : drift === 'in_sync' ? (
            <span className="pill pill--green pill--lg">
              <Icon name="checkCircle" size={15} strokeWidth={2.2} /> Watching
            </span>
          ) : (
            <DriftPill state={drift} large />
          )}
        </div>
        <div className="stat__sub">{healText(r)}</div>
        {failedHeal.length > 0 && (
          <button className="abtn abtn--red stat__action" onClick={() => onRetry(failedHeal.map(t => t.env))}>
            <Icon name="refresh" size={13} /> Retry sync
          </button>
        )}
      </section>
      <section className="card stat">
        <div className="stat__label">Last sync</div>
        <div className="stat__value">{op ? <OpPill state={op.state} large /> : <span className="pill pill--gray pill--lg">Never</span>}</div>
        <div className="stat__sub">
          {op ? (
            <>
              {last.env} · <Timestamp iso={op.finishedAt ?? op.startedAt} /> · {TRIGGER[op.trigger]}
              {last.revision && (
                <>
                  {' '}
                  · to <span className="mono">{last.revision.slice(0, 7)}</span>
                </>
              )}
              <Link to={taskHref(op.taskId)} className="stat__link">
                {op.error ?? (op.state === 'SUCCEEDED' ? `${op.written} key${op.written === 1 ? '' : 's'} written${op.pruned ? `, ${op.pruned} pruned` : ''}` : op.message)}
              </Link>
            </>
          ) : (
            'Nothing has been written by varTrack yet.'
          )}
        </div>
      </section>
    </div>
  );
}

function RuleOverview({ rule: r }: { rule: RuleView }) {
  return (
    <div className="stack" style={{ gap: 20 }}>
      <dl className="facts">
        <div>
          <dt>Platform</dt>
          <dd>{r.platform}</dd>
        </div>
        <div>
          <dt>Datasource</dt>
          <dd>
            <span className="mono">{r.datasource}</span> <span className="muted">({SINK_LABEL[r.sinkType]})</span>
          </dd>
        </div>
        <div>
          <dt>Repositories</dt>
          <dd>{repoSummary(r)}</dd>
        </div>
        <div>
          <dt>File</dt>
          <dd className="mono">{fileSummary(r)}</dd>
        </div>
        <div>
          <dt>Destination template</dt>
          <dd className="mono">{r.destinationTemplate}</dd>
        </div>
        <div>
          <dt>Sync mode</dt>
          <dd className="mono">{r.syncMode}</dd>
        </div>
        <div>
          <dt>Self-heal</dt>
          <dd>{r.selfHeal ? 'On — drift is restored from Git automatically' : "Off — the watcher doesn't poll this rule"}</dd>
        </div>
        <div>
          <dt>Prune</dt>
          <dd>
            {r.prune.enabled ? `On${r.prune.dryRun ? ' (report only)' : ''}` : 'Off'}
            {r.prune.protection.length > 0 && (
              <span className="muted">
                {' '}
                · protects {r.prune.protection.map(p => <code key={p} style={{ marginRight: 4 }}>{p}</code>)}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt>Secret manager</dt>
          <dd>{r.secretManager ?? <span className="muted">none</span>}</dd>
        </div>
      </dl>
      <div className="stack stack--tight">
        <span className="section-label">How environments are resolved</span>
        <p className="muted" style={{ fontSize: 13 }}>
          When a push arrives, varTrack picks the environment from the first step that matches.
        </p>
        <ResolutionSteps rule={r} />
      </div>
    </div>
  );
}

function RuleHistory({ rule }: { rule: RuleView }) {
  const tasks = useQuery(`tasks:${rule.name}`, api => api.listTasks({ rule: rule.name, limit: 100 }));
  const [env, setEnv] = useState('all');
  if (tasks.error && !tasks.data) return <ErrorState compact error={tasks.error} what="sync history" onRetry={tasks.reload} />;
  if (!tasks.data) return <LoadingRows rows={3} height={60} />;
  const shown = tasks.data.filter(t => env === 'all' || t.env === env);
  return (
    <div className="stack">
      {rule.targets.length > 1 && (
        <Segmented label="Environment" value={env} onChange={setEnv} options={[{ value: 'all', label: 'All' }, ...rule.targets.map(t => ({ value: t.env, label: t.env }))]} />
      )}
      {shown.length ? <HistoryTimeline tasks={shown} showEnv={env === 'all'} /> : <EmptyState compact icon="tasks" title="No syncs yet" />}
    </div>
  );
}

function RuleEvents({ rule }: { rule: RuleView }) {
  const events = useQuery(`events:${rule.name}`, api => api.listEvents({ rule: rule.name, limit: 200 }));
  if (events.error && !events.data) return <ErrorState compact error={events.error} what="events" onRetry={events.reload} />;
  if (!events.data) return <LoadingRows rows={4} height={30} />;
  if (events.data.length === 0) return <EmptyState compact icon="activity" title="No events for this rule" />;
  return <div>{asGroups(events.data).map(g => <EventRow key={g.event.id} group={g} showSubject={false} />)}</div>;
}

function RuleDiff({ rule }: { rule: RuleView }) {
  const targets = [...rule.targets].sort((a, b) => SYNC_RANK[targetSync(a)] - SYNC_RANK[targetSync(b)]);
  return (
    <div className="stack">
      <p className="muted" style={{ fontSize: 13 }}>
        Current state in {SINK_LABEL[rule.sinkType]} on the left, desired state in Git on the right. Inline, <span className="mono">−</span> is current and{" "}
        <span className="mono">+</span> is desired.
      </p>
      <DiffOptions />
      {targets.map(t => (
        <section key={t.env} className="stack stack--tight">
          <span className="row" style={{ gap: 8 }}>
            <span className="section-label">{t.env}</span>
            <SyncPill status={targetSync(t)} />
          </span>
          <EnvDiff rule={rule.name} env={t.env} destination={t.destination} />
        </section>
      ))}
    </div>
  );
}
