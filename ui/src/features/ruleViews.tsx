import { Link } from 'react-router-dom';
import type { RuleView } from '../api/types';
import { Timestamp } from '../components/common';
import { Icon, SINK_ICON, SINK_LABEL } from '../components/Icon';
import { NameLabel, Star } from '../components/Row';
import { DriftPill, OpPill, SyncIcon, SyncPill, TONE_COLOR } from '../components/Status';
import { filesRead, runRights } from '../lib/rules';
import { DRIFT, latestSync, ruleDrift, ruleSync, ruleSyncState, SYNC, targetNote, targetSync, TRIGGER } from '../lib/status';
import { envHref, ruleHref } from './links';

export function repoSummary(rule: RuleView): string {
  const repos = rule.repositories.join(', ');
  return rule.excludeRepositories.length ? `${repos} (except ${rule.excludeRepositories.join(', ')})` : repos;
}

export const fileSummary = filesRead;

/** Color name for the rule's overall state: worst drift, or red for a failed last sync. */
export function ruleColor(rule: RuleView): string {
  const tone = DRIFT[ruleDrift(rule)].tone;
  if (rule.targets.some(t => t.lastSync?.state === 'FAILED') && (tone === 'good' || tone === 'suspended' || tone === 'neutral')) return 'red';
  return TONE_COLOR[tone];
}

/** One line saying what matters most about the rule right now. */
export function ruleMessage(rule: RuleView): string {
  const problem = rule.targets.find(t => t.drift === 'heal_failed') ?? rule.targets.find(t => t.drift === 'drifted' || t.drift === 'healing' || t.drift === 'unknown');
  if (problem) {
    const keys = problem.driftedKeys ? `${problem.driftedKeys} key${problem.driftedKeys === 1 ? '' : 's'} differ from Git` : DRIFT[problem.drift].label.toLowerCase();
    return `${problem.env}: ${problem.drift === 'unknown' ? problem.driftMessage ?? keys : keys}`;
  }
  const failed = rule.targets.find(t => t.lastSync?.state === 'FAILED');
  if (failed) return `${failed.env}: last sync failed — ${failed.lastSync?.error ?? failed.lastSync?.message}`;
  const watched = rule.targets.filter(t => t.drift !== 'not_watched');
  if (watched.length === 0) return `${rule.targets.length} environment${rule.targets.length === 1 ? '' : 's'} · not watched for drift`;
  return `${watched.length === rule.targets.length ? 'All' : `${watched.length} of ${rule.targets.length}`} environments match Git`;
}

const ENV_LIMIT = 5;

/** A rule that names no environments was refused when it was loaded: nothing runs it. It is listed, with why, and has no actions. */
function RefusedCard({ rule, pinned, onTogglePin, showTenant }: { rule: RuleView; pinned: boolean; onTogglePin: () => void; showTenant?: boolean }) {
  return (
    <article className="card rcard rcard--red" aria-label={`${rule.name}: Refused`}>
      <header className="rcard__head">
        <span className="rcard__kind">
          <Icon name={SINK_ICON[rule.sinkType]} size={14} />
          {SINK_LABEL[rule.sinkType]}
          {showTenant && <span className="tag" title="Tenant">{rule.tenant}</span>}
        </span>
        <Star active={pinned} onToggle={onTogglePin} label={rule.name} />
      </header>
      <h3 className="rcard__name">
        <NameLabel prefix={rule.platform} name={rule.datasource} to={ruleHref(rule)} />
      </h3>
      <div className="rcard__pills">
        <span className="pill pill--red">Refused</span>
      </div>
      <p className="secondary" style={{ margin: 0 }} data-testid="refused-reason">
        {rule.refused}
      </p>
      <footer className="rcard__actions">
        <Link className="abtn rcard__open" to={ruleHref(rule)}>
          Details <Icon name="arrowRight" size={13} />
        </Link>
      </footer>
    </article>
  );
}

/** A rule as a box: overall sync status, desired vs current, and one line per environment. */
export function RuleCard({ rule, pinned, onTogglePin, onSync, showTenant }: { rule: RuleView; pinned: boolean; onTogglePin: () => void; onSync: (dryRun?: boolean) => void; showTenant?: boolean }) {
  if (rule.refused) return <RefusedCard rule={rule} pinned={pinned} onTogglePin={onTogglePin} showTenant={showTenant} />;
  const sync = ruleSync(rule);
  const rights = runRights(rule);
  const last = latestSync(rule.targets);
  const op = ruleSyncState(rule);
  const heal = rule.targets.some(t => t.drift === 'heal_failed') ? 'heal_failed' : rule.targets.some(t => t.drift === 'healing') ? 'healing' : undefined;
  const envs = rule.targets.slice(0, ENV_LIMIT);
  return (
    <article className={`card rcard rcard--${TONE_COLOR[SYNC[sync].tone]}`} aria-label={`${rule.name}: ${SYNC[sync].label}`}>
      <header className="rcard__head">
        <span className="rcard__kind">
          <Icon name={SINK_ICON[rule.sinkType]} size={14} />
          {SINK_LABEL[rule.sinkType]}
          {showTenant && <span className="tag" title="Tenant">{rule.tenant}</span>}
        </span>
        <Star active={pinned} onToggle={onTogglePin} label={rule.name} />
      </header>
      <h3 className="rcard__name">
        <NameLabel prefix={rule.platform} name={rule.datasource} to={ruleHref(rule)} />
      </h3>
      <div className="rcard__pills">
        <SyncPill status={sync} />
        {heal && <DriftPill state={heal} />}
        {op !== 'never' && op !== 'SUCCEEDED' && <OpPill state={op} />}
      </div>
      <dl className="rcard__facts">
        <div>
          <dt>Desired</dt>
          <dd title={`${repoSummary(rule)} · ${fileSummary(rule)}`}>
            <Icon name="gitBranch" size={13} className="faint" />
            <span className="truncate">
              {repoSummary(rule)} · <span className="mono">{fileSummary(rule)}</span>
            </span>
          </dd>
        </div>
        <div>
          <dt>Current</dt>
          <dd title={`${SINK_LABEL[rule.sinkType]} · ${rule.destinationTemplate}`}>
            <Icon name={SINK_ICON[rule.sinkType]} size={13} className="faint" />
            <span className="truncate mono">{rule.destinationTemplate}</span>
          </dd>
        </div>
        <div>
          <dt>Last sync</dt>
          <dd>
            <Icon name="clock" size={13} className="faint" />
            {last ? (
              <span className="truncate">
                <Timestamp iso={last.finishedAt ?? last.startedAt} /> · {TRIGGER[last.trigger]}
              </span>
            ) : (
              <span className="muted">never</span>
            )}
          </dd>
        </div>
      </dl>
      <ul className="rcard__envs" aria-label="Environments">
        {envs.map(t => (
          <li key={t.env}>
            <Link to={envHref(rule, t.env)} className="rcard__env">
              <SyncIcon status={targetSync(t)} size={14} />
              <span className="rcard__env-name">{t.env}</span>
              <span className={`rcard__env-note${targetSync(t) === 'OutOfSync' || t.lastSync?.state === 'FAILED' ? ' rcard__env-note--warn' : ''}`}>{targetNote(t)}</span>
            </Link>
          </li>
        ))}
        {rule.targets.length > ENV_LIMIT && (
          <li>
            <Link to={ruleHref(rule)} className="rcard__env rcard__more">
              +{rule.targets.length - ENV_LIMIT} more
            </Link>
          </li>
        )}
      </ul>
      <footer className="rcard__actions">
        {rights.sync ? (
          <button type="button" className="abtn abtn--blue" onClick={() => onSync()}>
            <Icon name="refresh" size={13} /> Sync
          </button>
        ) : (
          // They may read this rule's repository, not write to it: Sync stays, disabled, and says why; a dry run is its own button.
          <>
            <button type="button" className="abtn" disabled title={rights.reason}>
              <Icon name="refresh" size={13} /> Sync
            </button>
            <button type="button" className="abtn" onClick={() => onSync(true)}>
              <Icon name="play" size={13} /> Dry run
            </button>
          </>
        )}
        <Link className="abtn rcard__open" to={ruleHref(rule)}>
          Details <Icon name="arrowRight" size={13} />
        </Link>
      </footer>
    </article>
  );
}
