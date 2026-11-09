import { Link } from 'react-router-dom';
import type { RuleView, Target } from '../api/types';
import { Timestamp } from '../components/common';
import { Icon, SINK_LABEL } from '../components/Icon';
import { ExpandRow, NameLabel } from '../components/Row';
import { SyncIcon } from '../components/Status';
import { diffable, targetNote, targetSync } from '../lib/status';
import { envHref } from './links';
import { EnvCompare, EnvRun, TargetPills } from './state';

export function EnvRow({ rule, target: t, onSync, defaultOpen, showTenant }: { rule: RuleView; target: Target; onSync: (dryRun?: boolean) => void; defaultOpen?: boolean; showTenant?: boolean }) {
  const at = t.lastSync?.finishedAt ?? t.lastSync?.startedAt;
  const diff = diffable(t);
  return (
    <ExpandRow
      label={`${rule.name} ${t.env}`}
      defaultOpen={defaultOpen}
      icon={<SyncIcon status={targetSync(t)} />}
      title={<NameLabel prefix={showTenant ? `${rule.tenant} · ${rule.name}` : rule.name} name={t.env} to={envHref(rule, t.env)} />}
      status={<TargetPills target={t} />}
      meta={
        <>
          {targetNote(t)} · {t.ref} → {SINK_LABEL[rule.sinkType]} <span className="mono">{t.destination}</span>
          {at && (
            <>
              {' '}
              · last sync <Timestamp iso={at} />
            </>
          )}
        </>
      }
      details={() => (
        <div className="stack" style={{ gap: 12 }}>
          <EnvCompare rule={rule} target={t} />
          <div className="action-bar">
            <EnvRun rule={rule} target={t} onSync={onSync} />
            {diff.can ? (
              <Link className="abtn" to={envHref(rule, t.env, 'diff')}>
                <Icon name="diff" size={13} /> Diff
              </Link>
            ) : (
              // Nothing to compare (never written, or no branch feeds it any more): Diff stays, disabled, and says why.
              <button type="button" className="abtn" disabled title={diff.reason}>
                <Icon name="diff" size={13} /> Diff
              </button>
            )}
            {diff.can && (
              <Link className="abtn" to={envHref(rule, t.env, 'current')}>
                <Icon name="key" size={13} /> Current values
              </Link>
            )}
          </div>
        </div>
      )}
    />
  );
}
