import { Link } from 'react-router-dom';
import type { RuleView, Target } from '../api/types';
import { Timestamp } from '../components/common';
import { Icon, SINK_LABEL } from '../components/Icon';
import { ExpandRow, NameLabel } from '../components/Row';
import { SyncIcon } from '../components/Status';
import { targetNote, targetSync } from '../lib/status';
import { envHref } from './links';
import { EnvCompare, TargetPills } from './state';

export function EnvRow({ rule, target: t, onSync, defaultOpen, showTenant }: { rule: RuleView; target: Target; onSync: () => void; defaultOpen?: boolean; showTenant?: boolean }) {
  const at = t.lastSync?.finishedAt ?? t.lastSync?.startedAt;
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
            <button className="abtn abtn--blue" onClick={onSync}>
              <Icon name="refresh" size={13} /> Sync {t.env}
            </button>
            <Link className="abtn" to={envHref(rule, t.env, 'diff')}>
              <Icon name="diff" size={13} /> Diff
            </Link>
            <Link className="abtn" to={envHref(rule, t.env, 'current')}>
              <Icon name="key" size={13} /> Current values
            </Link>
          </div>
        </div>
      )}
    />
  );
}
