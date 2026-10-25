import { CopyButton, EmptyState, ErrorState, LoadingRows } from '../../components/common';
import { Icon } from '../../components/Icon';
import { diffCounts, hasDifferences, keyChanges, unifiedLines, unifiedText, type KeyChange } from '../../lib/diff';
import { usePrefs } from '../../lib/prefs';
import { useQuery } from '../../lib/useQuery';

/** Changed-only and inline switches, shared by every diff on the page. */
export function DiffOptions() {
  const [prefs, setPrefs] = usePrefs();
  return (
    <div className="row row--wrap" style={{ gap: 16 }}>
      <label className="check" style={{ padding: 0 }}>
        <input type="checkbox" checked={prefs.diffCompact} onChange={e => setPrefs({ diffCompact: e.target.checked })} />
        <span>Changed keys only</span>
      </label>
      <label className="check" style={{ padding: 0 }}>
        <input type="checkbox" checked={prefs.diffUnified} onChange={e => setPrefs({ diffUnified: e.target.checked })} />
        <span>Inline</span>
      </label>
    </div>
  );
}

export function DiffSummary({ changes }: { changes: KeyChange[] }) {
  const c = diffCounts(changes);
  if (!hasDifferences(changes)) return <span className="muted">matches Git</span>;
  const parts = [
    c.changed && `${c.changed} changed`,
    c.missing && `${c.missing} missing from sink`,
    c.onlyInSink && `${c.onlyInSink} only in sink`,
  ].filter(Boolean);
  return <span className="secondary">{parts.join(' · ')}</span>;
}

/** Current state (sink) against desired state (Git) for one environment. */
export function EnvDiff({ rule, tenant, env, destination }: { rule: string; tenant?: string; env: string; destination: string }) {
  const keys = useQuery(`keys:${tenant}:${rule}:${env}`, api => api.listKeys(rule, env, tenant));
  const [prefs] = usePrefs();

  if (keys.error && !keys.data) return <ErrorState compact error={keys.error} what="key values" onRetry={keys.reload} />;
  if (!keys.data) return <LoadingRows rows={4} height={22} />;

  const changes = keyChanges(keys.data);
  return (
    <div className="diff">
      <div className="diff__header">
        <Icon name="diff" size={14} className="muted" />
        <span className="mono truncate">{destination}</span>
        <DiffSummary changes={changes} />
        <span className="spacer" />
        {hasDifferences(changes) && <CopyButton text={unifiedText(destination, changes)} label="Copy diff" />}
      </div>
      {!hasDifferences(changes) && prefs.diffCompact ? (
        <EmptyState compact icon="checkCircle" title="No differences">
          Every key in <code>{destination}</code> matches Git.
        </EmptyState>
      ) : prefs.diffUnified ? (
        <UnifiedDiff changes={changes} compact={prefs.diffCompact} />
      ) : (
        <SplitDiff changes={changes} compact={prefs.diffCompact} />
      )}
    </div>
  );
}

function UnifiedDiff({ changes, compact }: { changes: KeyChange[]; compact: boolean }) {
  const lines = unifiedLines(changes, compact);
  return (
    <div className="diff__body" role="table" aria-label="Inline diff: minus is the current value in the sink, plus is the desired value in Git">
      {lines.map((l, i) => (
        <div key={`${l.key}:${l.sign}:${i}`} role="row" className={`diff__line${l.sign === '-' ? ' diff__line--del' : l.sign === '+' ? ' diff__line--add' : ''}`}>
          <span className="diff__sign" aria-label={l.sign === '-' ? 'current' : l.sign === '+' ? 'desired' : 'unchanged'}>
            {l.sign}
          </span>
          <span className="diff__text">{l.text}</span>
        </div>
      ))}
    </div>
  );
}

function SplitDiff({ changes, compact }: { changes: KeyChange[]; compact: boolean }) {
  const rows = compact ? changes.filter(c => c.kind !== 'same') : changes;
  return (
    <div className="table-wrap">
      <table className="table table--compact diff__split">
        <thead>
          <tr>
            <th>Key</th>
            <th>Current · sink</th>
            <th>Desired · Git</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(c => (
            <tr key={c.key} className={c.kind === 'same' ? undefined : 'row--drifted'}>
              <td className="mono">{c.key}</td>
              <td className={c.kind !== 'same' && c.live !== undefined ? 'diff__cell--del' : undefined}>
                {c.live === undefined ? <span className="value value--absent">absent</span> : <span className="value">{c.live}</span>}
              </td>
              <td className={c.kind !== 'same' && c.git !== undefined ? 'diff__cell--add' : undefined}>
                {c.git === undefined ? <span className="value value--absent">absent</span> : <span className="value">{c.git}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
