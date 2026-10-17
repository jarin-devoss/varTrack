import { Link } from 'react-router-dom';
import type { EventReason, VtEvent } from '../api/types';
import { Timestamp } from '../components/common';
import { ExpandRow, NameLabel } from '../components/Row';
import { StatusIcon } from '../components/Status';
import type { IconName } from '../components/Icon';
import type { Tone } from '../lib/status';
import { formatDateTime } from '../lib/time';
import { envHref, ruleHref, taskHref } from './links';

export const REASON: Record<EventReason, { label: string; icon: IconName; tone: Tone }> = {
  WebhookReceived: { label: 'Push received', icon: 'webhook', tone: 'neutral' },
  SyncStarted: { label: 'Sync started', icon: 'loader', tone: 'progress' },
  SyncSucceeded: { label: 'Synced', icon: 'checkCircle', tone: 'good' },
  SyncFailed: { label: 'Sync failed', icon: 'xCircle', tone: 'critical' },
  ValidationFailed: { label: 'Validation failed', icon: 'xCircle', tone: 'critical' },
  KeysPruned: { label: 'Keys pruned', icon: 'trash', tone: 'neutral' },
  DriftDetected: { label: 'Drift detected', icon: 'arrowUpCircle', tone: 'warning' },
  HealSucceeded: { label: 'Healed', icon: 'refresh', tone: 'good' },
  HealFailed: { label: 'Heal failed', icon: 'xCircle', tone: 'critical' },
  SchemaRefreshed: { label: 'Schema refreshed', icon: 'file', tone: 'neutral' },
};

export interface EventGroup {
  /** The most recent occurrence. */
  event: VtEvent;
  count: number;
  firstSeen: string;
}

/** Fold identical events (same reason, subject and message) into one row. */
export function groupEvents(events: VtEvent[]): EventGroup[] {
  const groups = new Map<string, EventGroup>();
  for (const e of events) {
    const key = `${e.reason}|${e.rule ?? ''}|${e.env ?? ''}|${e.message}`;
    const g = groups.get(key);
    if (!g) {
      groups.set(key, { event: e, count: 1, firstSeen: e.time });
    } else {
      g.count++;
      if (e.time > g.event.time) g.event = e;
      if (e.time < g.firstSeen) g.firstSeen = e.time;
    }
  }
  return [...groups.values()].sort((a, b) => b.event.time.localeCompare(a.event.time));
}

export const asGroups = (events: VtEvent[]): EventGroup[] => events.map(e => ({ event: e, count: 1, firstSeen: e.time }));

export function EventRow({ group, showSubject = true }: { group: EventGroup; showSubject?: boolean }) {
  const e = group.event;
  const r = REASON[e.reason];
  const [prefix, name] = subject(e);
  return (
    <ExpandRow
      label={`${r.label} ${e.message}`}
      icon={<StatusIcon icon={r.icon} tone={r.tone} spin={e.reason === 'SyncStarted'} title={r.label} />}
      title={
        <>
          <span className="rowx__name">
            <strong>{r.label}</strong>
          </span>
          {showSubject && name && (
            <>
              <span className="rowx__prefix"> · </span>
              <NameLabel prefix={prefix} name={name} to={e.rule ? (e.env ? envHref(e.rule, e.env) : ruleHref(e.rule)) : undefined} />
            </>
          )}
        </>
      }
      status={
        <>
          {group.count > 1 && (
            <span className="tag num" title={`Seen ${group.count} times`}>
              ×{group.count}
            </span>
          )}
          {e.type === 'Warning' && <span className="pill pill--red">Warning</span>}
        </>
      }
      meta={
        <>
          {e.message} · <Timestamp iso={e.time} />
        </>
      }
      details={() => (
        <dl className="facts">
          <div>
            <dt>Message</dt>
            <dd>{e.message}</dd>
          </div>
          <div>
            <dt>{group.count > 1 ? 'First / last seen' : 'Time'}</dt>
            <dd>
              {group.count > 1 && <>{formatDateTime(group.firstSeen)} → </>}
              {formatDateTime(e.time)}
            </dd>
          </div>
          {e.rule && (
            <div>
              <dt>Rule</dt>
              <dd>
                <Link to={ruleHref(e.rule)} className="mono">
                  {e.rule}
                </Link>
                {e.env && <> · {e.env}</>}
              </dd>
            </div>
          )}
          {e.taskId && (
            <div>
              <dt>Task</dt>
              <dd>
                <Link to={taskHref(e.taskId)} className="mono">
                  {e.taskId}
                </Link>
              </dd>
            </div>
          )}
        </dl>
      )}
    />
  );
}

function subject(e: VtEvent): [string | undefined, string] {
  if (e.rule) return e.env ? [e.rule, e.env] : [undefined, e.rule];
  return [undefined, e.env ?? 'tenant'];
}
