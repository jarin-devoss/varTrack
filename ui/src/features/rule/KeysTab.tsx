import { useMemo, useState } from 'react';
import type { KeyEntry, KeyState, RuleView } from '../../api/types';
import { EmptyState, ErrorState, LoadingRows, Notice, Segmented } from '../../components/common';
import { Icon } from '../../components/Icon';
import { SearchBox } from '../../components/SearchBox';
import { StatusIcon } from '../../components/Status';
import type { Tone } from '../../lib/status';
import { useQuery } from '../../lib/useQuery';

export type KeyFilter = 'all' | 'changed' | 'secrets' | 'protected';

const STATE: Record<KeyState, { label: string; tone: Tone; icon: 'checkCircle' | 'alertTriangle' | 'minusCircle' | 'helpCircle' }> = {
  synced: { label: 'Matches Git', tone: 'good', icon: 'checkCircle' },
  drifted: { label: 'Changed in sink', tone: 'warning', icon: 'alertTriangle' },
  missing: { label: 'Missing from sink', tone: 'warning', icon: 'minusCircle' },
  unmanaged: { label: 'Not in Git', tone: 'serious', icon: 'helpCircle' },
};

interface Props {
  rule: RuleView;
  env: string;
  onEnvChange: (env: string) => void;
  filter: KeyFilter;
  onFilterChange: (f: KeyFilter) => void;
  onSync: (env: string) => void;
}

export function KeysTab({ rule, env, onEnvChange, filter, onFilterChange, onSync }: Props) {
  const keys = useQuery(`keys:${rule.name}:${env}`, api => api.listKeys(rule.name, env));
  const [search, setSearch] = useState('');
  const target = rule.targets.find(t => t.env === env);

  const all = keys.data ?? [];
  const counts = {
    drifted: all.filter(k => k.state === 'drifted').length,
    missing: all.filter(k => k.state === 'missing').length,
    unmanaged: all.filter(k => k.state === 'unmanaged').length,
  };
  const changed = counts.drifted + counts.missing + counts.unmanaged;

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return all.filter(k => {
      if (filter === 'changed' && k.state === 'synced') return false;
      if (filter === 'secrets' && !k.secret) return false;
      if (filter === 'protected' && !k.protected) return false;
      if (!q) return true;
      return k.key.toLowerCase().includes(q) || (!k.secret && `${k.gitValue ?? ''} ${k.liveValue ?? ''}`.toLowerCase().includes(q));
    });
  }, [all, filter, search]);

  return (
    <div className="stack">
      <div className="filter-form">
        {rule.targets.length > 1 && (
          <Segmented label="Environment" value={env} onChange={onEnvChange} options={rule.targets.map(t => ({ value: t.env, label: t.env }))} />
        )}
        <Segmented
          label="Show"
          value={filter}
          onChange={onFilterChange}
          options={[
            { value: 'all', label: 'All' },
            { value: 'changed', label: `Changed${changed ? ` (${changed})` : ''}` },
            { value: 'secrets', label: 'Secrets' },
            { value: 'protected', label: 'Protected' },
          ]}
        />
        <SearchBox value={search} onChange={setSearch} placeholder="Search keys and values…" />
      </div>

      {target && keys.data && (
        <p className="secondary">
          <strong className="num">{all.length - counts.unmanaged}</strong> keys from Git in <code>{target.destination}</code>
          {changed > 0 ? (
            <>
              {' '}· {[counts.drifted && `${counts.drifted} changed in the sink`, counts.missing && `${counts.missing} missing`, counts.unmanaged && `${counts.unmanaged} not in Git`].filter(Boolean).join(', ')}
            </>
          ) : (
            ' · everything matches Git'
          )}
        </p>
      )}

      {changed > 0 && target && (
        <Notice tone="warning">
          <div className="row row--wrap" style={{ gap: 8 }}>
            <span style={{ flex: 1, minWidth: 240 }}>
              {rule.selfHeal ? 'Self-heal restores changed and missing keys from Git.' : 'Self-heal is off for this rule — sync to restore Git values.'}{' '}
              {counts.unmanaged > 0 &&
                (rule.prune.enabled
                  ? 'Keys not in Git are deleted when you sync with prune.'
                  : 'Keys not in Git are kept because pruning is off.')}
            </span>
            <button className="btn btn--sm" onClick={() => onSync(env)}>
              <Icon name="refresh" size={14} /> Sync {env}
            </button>
          </div>
        </Notice>
      )}

      <div className="diff">
        {keys.error && !keys.data ? (
          <ErrorState error={keys.error} what="key values" onRetry={keys.reload} />
        ) : !keys.data ? (
          <div className="card__body">
            <LoadingRows rows={6} height={30} />
          </div>
        ) : shown.length === 0 ? (
          <EmptyState compact icon={filter === 'changed' ? 'checkCircle' : 'search'} title={filter === 'changed' && !search ? 'No changed keys' : 'No keys match'}>
            {filter === 'changed' && !search ? 'The sink matches Git for this environment.' : 'Try another filter or search.'}
          </EmptyState>
        ) : (
          <KeyTable keys={shown} protection={rule.prune.protection} />
        )}
      </div>
    </div>
  );
}

function KeyTable({ keys, protection }: { keys: KeyEntry[]; protection: string[] }) {
  return (
    <div className="table-wrap">
      <table className="table table--compact">
        <thead>
          <tr>
            <th>Key</th>
            <th>Current · sink</th>
            <th>Desired · Git</th>
            <th>State</th>
          </tr>
        </thead>
        <tbody>
          {keys.map(k => {
            const s = STATE[k.state];
            const differs = k.state !== 'synced';
            return (
              <tr key={k.key} className={k.state === 'drifted' ? 'row--drifted' : undefined}>
                <td>
                  <span className="row" style={{ gap: 6 }}>
                    <span className="mono">{k.key}</span>
                    {k.secret && <Icon name="lock" size={13} className="muted" label="Secret — value resolved from the secret manager" />}
                    {k.protected && (
                      <Icon name="shield" size={13} className="muted" label={`Protected from prune (${protection.join(', ')})`} />
                    )}
                  </span>
                </td>
                <td>
                  <Value value={k.liveValue} absent="not in sink" changed={differs && k.liveValue !== undefined} side="live" />
                </td>
                <td>
                  <Value value={k.gitValue} absent="not in Git" changed={differs && k.gitValue !== undefined} side="git" />
                </td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <span className="badge">
                    <StatusIcon icon={s.icon} tone={s.tone} size={14} />
                    {s.label}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Value({ value, absent, changed, side }: { value?: string; absent: string; changed: boolean; side: 'git' | 'live' }) {
  if (value === undefined) return <span className="value value--absent">{absent}</span>;
  const cls = changed ? (side === 'git' ? ' value--git-changed' : ' value--live-changed') : '';
  return <span className={`value${cls}`}>{value}</span>;
}
