import { Link } from 'react-router-dom';
import type { KeyEntry, RuleView, Target } from '../api/types';
import { CopyButton, EmptyState, ErrorState, LoadingRows, Revision, Timestamp } from '../components/common';
import { Icon, SINK_ICON, SINK_LABEL } from '../components/Icon';
import { SlideOver } from '../components/Overlay';
import { UTabs } from '../components/Panel';
import { DriftPill, OpPill, SyncIcon, SyncPill, TONE_COLOR } from '../components/Status';
import { DRIFT, SYNC, TRIGGER, targetNote, targetSync } from '../lib/status';
import { useQuery } from '../lib/useQuery';
import { taskHref } from './links';
import { DiffOptions, EnvDiff } from './rule/DiffView';
import { HistoryTimeline } from './rule/HistoryTimeline';
import { resolvedVia } from './rule/resolution';

/** The config file an environment is read from. */
export function targetFile(rule: RuleView, t: Target): string {
  if (rule.fileName) return rule.fileName;
  const match = Object.entries(rule.filePathMap).find(([, env]) => env === t.env);
  return match ? match[0] : Object.keys(rule.filePathMap)[0] ?? '—';
}

/** One sentence about the current state compared with Git. */
function currentLine(rule: RuleView, t: Target) {
  if (t.drift === 'drifted' || t.drift === 'healing' || t.drift === 'heal_failed') {
    const keys = t.driftedKeys ? `${t.driftedKeys} of ${t.keyCount} keys differ from Git` : 'differs from Git';
    return (
      <>
        {keys}
        {t.driftDetectedAt && (
          <span className="faint">
            {' '}
            · since <Timestamp iso={t.driftDetectedAt} />
          </span>
        )}
      </>
    );
  }
  if (t.drift === 'in_sync') return <>matches Git · {t.keyCount} keys</>;
  if (t.drift === 'unknown') return <>couldn't be read{t.driftMessage ? ` — ${t.driftMessage}` : ''}</>;
  return <>not compared{rule.selfHeal ? '' : ' — self-heal is off'}</>;
}

/** Pills that say more than the sync status: a heal in progress or given up, a sync running or failed. */
export function TargetPills({ target: t }: { target: Target }) {
  return (
    <>
      <SyncPill status={targetSync(t)} />
      {(t.drift === 'healing' || t.drift === 'heal_failed') && <DriftPill state={t.drift} />}
      {t.lastSync && t.lastSync.state !== 'SUCCEEDED' && <OpPill state={t.lastSync.state} />}
    </>
  );
}

/** Desired state in Git on the left, current state in the sink on the right. */
export function EnvCompare({ rule, target: t }: { rule: RuleView; target: Target }) {
  const sync = targetSync(t);
  const sink = SINK_LABEL[rule.sinkType];
  return (
    <div className={`cmp cmp--${TONE_COLOR[SYNC[sync].tone]}`}>
      <section className="cmp__side" aria-label="Desired state, in Git">
        <div className="cmp__label">
          <Icon name="gitBranch" size={13} /> Desired · Git
        </div>
        <div className="cmp__main mono truncate" title={`${t.repository}@${t.ref}`}>
          {t.repository}
          <span className="faint">@</span>
          {t.ref}
        </div>
        <div className="cmp__sub">
          <Revision sha={t.revision} />
          {t.commitMessage && <span className="truncate">{t.commitMessage}</span>}
        </div>
        <div className="cmp__sub">
          <Icon name="file" size={13} className="faint" />
          <span className="mono truncate">{targetFile(rule, t)}</span>
        </div>
      </section>

      <div className="cmp__link" aria-hidden="true">
        <span className="cmp__wire" />
        <SyncIcon status={sync} size={18} />
        <span className="cmp__wire cmp__wire--head" />
      </div>

      <section className="cmp__side" aria-label={`Current state, in ${sink}`}>
        <div className="cmp__label">
          <Icon name={SINK_ICON[rule.sinkType]} size={13} /> Current · {sink}
        </div>
        <div className="cmp__main mono truncate" title={t.destination}>
          {t.destination}
        </div>
        <div className={`cmp__sub${sync === 'OutOfSync' ? ' cmp__sub--warn' : ''}`}>
          <span className="truncate">{currentLine(rule, t)}</span>
        </div>
        <div className="cmp__sub">
          {t.lastSync ? (
            <Link to={taskHref(t.lastSync.taskId)} className="cmp__task">
              last written <Timestamp iso={t.lastSync.finishedAt ?? t.lastSync.startedAt} /> · {TRIGGER[t.lastSync.trigger]}
            </Link>
          ) : (
            <span className="faint">never written by varTrack</span>
          )}
        </div>
      </section>
    </div>
  );
}

/** Error line under a box: why the last sync or self-heal failed, or why the sink can't be read. */
function targetProblem(t: Target): string | undefined {
  if (t.drift === 'heal_failed' || t.drift === 'unknown') return t.driftMessage;
  if (t.lastSync?.state === 'FAILED') return t.lastSync.error ?? t.lastSync.message;
  return undefined;
}

export type EnvView = 'summary' | 'diff' | 'current' | 'desired' | 'history';
export const ENV_VIEWS: EnvView[] = ['summary', 'diff', 'current', 'desired', 'history'];

/** One environment as a box: name and status on top, desired vs current underneath. */
export function EnvBox({ rule, target: t, onOpen, onSync }: { rule: RuleView; target: Target; onOpen: (view?: EnvView) => void; onSync: () => void }) {
  const sync = targetSync(t);
  const problem = targetProblem(t);
  return (
    <article className={`envbox envbox--${TONE_COLOR[SYNC[sync].tone]}`} aria-label={`${t.env}: ${SYNC[sync].label}`}>
      <header className="envbox__head">
        <button type="button" className="envbox__name" onClick={() => onOpen()} title="Open details">
          {t.env}
        </button>
        <span className="envbox__pills">
          <TargetPills target={t} />
        </span>
        <span className="envbox__note">{targetNote(t)}</span>
        <span className="envbox__actions">
          {sync === 'OutOfSync' && (
            <button type="button" className="abtn" onClick={() => onOpen('diff')}>
              <Icon name="diff" size={13} /> Diff
            </button>
          )}
          <button type="button" className="abtn abtn--blue" onClick={onSync}>
            <Icon name="refresh" size={13} /> Sync
          </button>
          <button type="button" className="icon-btn icon-btn--plain" onClick={() => onOpen()} aria-label={`Details for ${t.env}`} title="Details">
            <Icon name="chevronRight" size={16} />
          </button>
        </span>
      </header>
      <EnvCompare rule={rule} target={t} />
      {problem && (
        <div className="envbox__problem">
          <Icon name="alertCircle" size={14} style={{ flex: 'none', marginTop: 2 }} />
          <span>{problem}</span>
        </div>
      )}
    </article>
  );
}

/** Slide-over for one environment: summary, the diff, and both states in full. */
export function EnvPanel({
  rule,
  env,
  view,
  onView,
  onClose,
  onSync,
}: {
  rule: RuleView;
  env?: string;
  view: EnvView;
  onView: (v: EnvView) => void;
  onClose: () => void;
  onSync: (env: string) => void;
}) {
  const t = rule.targets.find(x => x.env === env);
  return (
    <SlideOver
      open={!!t}
      onClose={onClose}
      wide
      title={
        <span className="row" style={{ gap: 10 }}>
          {t?.env}
          {t && <SyncPill status={targetSync(t)} />}
        </span>
      }
      subtitle={
        <>
          {rule.name} · {SINK_LABEL[rule.sinkType]}
        </>
      }
      actions={
        t && (
          <button type="button" className="abtn abtn--blue" onClick={() => onSync(t.env)}>
            <Icon name="refresh" size={13} /> Sync {t.env}
          </button>
        )
      }
    >
      {t && (
        <>
          <UTabs
            label="Environment"
            active={view}
            onChange={onView}
            tabs={[
              { id: 'summary', label: 'Summary' },
              { id: 'diff', label: 'Diff' },
              { id: 'current', label: 'Current' },
              { id: 'desired', label: 'Desired' },
              { id: 'history', label: 'History' },
            ]}
          />
          {view === 'summary' && <EnvSummary rule={rule} target={t} />}
          {view === 'diff' && (
            <div className="stack">
              <p className="muted" style={{ fontSize: 13 }}>
                Left is the current state in {SINK_LABEL[rule.sinkType]}, right is the desired state in Git. A sync writes the right side.
              </p>
              <DiffOptions />
              <EnvDiff rule={rule.name} env={t.env} destination={t.destination} />
            </div>
          )}
          {view === 'current' && <StateValues rule={rule} target={t} side="live" />}
          {view === 'desired' && <StateValues rule={rule} target={t} side="git" />}
          {view === 'history' && <EnvHistory rule={rule} env={t.env} />}
        </>
      )}
    </SlideOver>
  );
}

function EnvSummary({ rule, target: t }: { rule: RuleView; target: Target }) {
  return (
    <div className="stack" style={{ gap: 20 }}>
      <EnvCompare rule={rule} target={t} />
      {targetProblem(t) && (
        <div className="envbox__problem envbox__problem--boxed">
          <Icon name="alertCircle" size={14} style={{ flex: 'none', marginTop: 2 }} />
          <span>{targetProblem(t)}</span>
        </div>
      )}
      <dl className="facts">
        <div>
          <dt>Environment resolved by</dt>
          <dd className="mono">{resolvedVia(rule, t)}</dd>
        </div>
        <div>
          <dt>Self-heal</dt>
          <dd className="stack" style={{ gap: 2 }}>
            <span>
              <DriftPill state={t.drift} />
            </span>
            <span className="muted" style={{ fontSize: 13 }}>
              {DRIFT[t.drift].description}
              {t.lastPolledAt && (
                <>
                  {' '}
                  Last checked <Timestamp iso={t.lastPolledAt} />.
                </>
              )}
            </span>
          </dd>
        </div>
        <div>
          <dt>Last sync</dt>
          <dd className="stack" style={{ gap: 2 }}>
            {t.lastSync ? (
              <>
                <span className="row" style={{ gap: 8 }}>
                  <OpPill state={t.lastSync.state} />
                  <Link to={taskHref(t.lastSync.taskId)} className="mono" style={{ fontSize: 12 }}>
                    {t.lastSync.taskId.slice(0, 8)}
                  </Link>
                </span>
                <span className="muted" style={{ fontSize: 13 }}>
                  {TRIGGER[t.lastSync.trigger]} · <Timestamp iso={t.lastSync.finishedAt ?? t.lastSync.startedAt} /> · {t.lastSync.written} written
                  {t.lastSync.pruned ? `, ${t.lastSync.pruned} pruned` : ''}
                </span>
              </>
            ) : (
              <span className="muted">never</span>
            )}
          </dd>
        </div>
        {t.author && (
          <div>
            <dt>Last commit by</dt>
            <dd>{t.author}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}

/** Every key of one side, as `key = value` lines; keys that differ from the other side are marked. */
function StateValues({ rule, target: t, side }: { rule: RuleView; target: Target; side: 'live' | 'git' }) {
  const keys = useQuery(`keys:${rule.name}:${t.env}`, api => api.listKeys(rule.name, t.env));
  if (keys.error && !keys.data) return <ErrorState compact error={keys.error} what="key values" onRetry={keys.reload} />;
  if (!keys.data) return <LoadingRows rows={6} height={20} />;

  const value = (k: KeyEntry) => (side === 'live' ? k.liveValue : k.gitValue);
  const rows = keys.data.filter(k => value(k) !== undefined).sort((a, b) => a.key.localeCompare(b.key));
  const differ = rows.filter(k => k.state !== 'synced').length;
  const text = rows.map(k => `${k.key} = ${value(k)}`).join('\n');
  const sink = SINK_LABEL[rule.sinkType];

  return (
    <div className="stack">
      <p className="secondary" style={{ fontSize: 13 }}>
        {side === 'live' ? (
          <>
            What is in {sink} at <code>{t.destination}</code> right now.
          </>
        ) : (
          <>
            What Git says should be there: <code>{targetFile(rule, t)}</code> at <code>{t.ref}</code>
            {t.revision && <> ({t.revision.slice(0, 7)})</>}.
          </>
        )}{' '}
        {differ > 0 && (
          <span className="c-yellow-text">
            {differ} key{differ === 1 ? '' : 's'} marked <span className="values__mark-sample" aria-hidden="true" /> differ from {side === 'live' ? 'Git' : sink}.
          </span>
        )}
      </p>
      {rows.length === 0 ? (
        <EmptyState compact icon="key" title={side === 'live' ? `Nothing in ${sink} yet` : 'No keys in Git'} />
      ) : (
        <div className="diff">
          <div className="diff__header">
            <Icon name={side === 'live' ? SINK_ICON[rule.sinkType] : 'gitBranch'} size={14} className="muted" />
            <span className="mono truncate">{side === 'live' ? t.destination : `${t.repository}@${t.ref}`}</span>
            <span className="muted num">{rows.length} keys</span>
            <span className="spacer" />
            <CopyButton text={text} label={side === 'live' ? 'Copy current values' : 'Copy desired values'} />
          </div>
          <div className="values" role="table" aria-label={side === 'live' ? `Current values in ${sink}` : 'Desired values in Git'}>
            {rows.map(k => (
              <div key={k.key} role="row" className={`values__line${k.state !== 'synced' ? ' values__line--differs' : ''}`}>
                <span className="values__key" role="cell">
                  {k.key}
                </span>
                <span className="values__eq" aria-hidden="true">
                  =
                </span>
                <span className="values__val" role="cell">
                  {value(k)}
                  {k.secret && <Icon name="lock" size={12} className="faint" label="Secret" style={{ marginLeft: 6, verticalAlign: -1 }} />}
                  {k.state !== 'synced' && <span className="sr-only"> (differs)</span>}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function EnvHistory({ rule, env }: { rule: RuleView; env: string }) {
  const tasks = useQuery(`tasks:${rule.name}`, api => api.listTasks({ rule: rule.name, limit: 100 }));
  if (tasks.error && !tasks.data) return <ErrorState compact error={tasks.error} what="sync history" onRetry={tasks.reload} />;
  if (!tasks.data) return <LoadingRows rows={3} height={60} />;
  const shown = tasks.data.filter(t => t.env === env);
  return shown.length ? <HistoryTimeline tasks={shown} showEnv={false} /> : <EmptyState compact icon="tasks" title={`No syncs to ${env} yet`} />;
}
