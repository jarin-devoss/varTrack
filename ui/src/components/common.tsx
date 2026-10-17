import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { isNotAvailable, type ApiError } from '../api/http';
import { formatDateTime, relativeTime } from '../lib/time';
import { useNow } from '../lib/useQuery';
import { Icon, type IconName } from './Icon';

export function Timestamp({ iso, prefix }: { iso?: string; prefix?: string }) {
  const now = useNow();
  if (!iso) return <span className="muted">—</span>;
  return (
    <time dateTime={iso} title={formatDateTime(iso)}>
      {prefix}
      {relativeTime(iso, now)}
    </time>
  );
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard can be blocked (insecure origin); the value stays selectable.
    }
  };
  return (
    <button type="button" className="btn btn--ghost btn--icon btn--sm" onClick={copy} title={copied ? 'Copied' : label} aria-label={label}>
      <Icon name={copied ? 'check' : 'copy'} size={14} />
    </button>
  );
}

export function Revision({ sha }: { sha?: string }) {
  if (!sha) return <span className="muted">—</span>;
  return (
    <span className="row" style={{ gap: 2 }}>
      <span className="mono" title={sha}>
        {sha.slice(0, 7)}
      </span>
      <CopyButton text={sha} label="Copy commit SHA" />
    </span>
  );
}

export function EmptyState({
  icon = 'search',
  title,
  children,
  action,
  compact,
}: {
  icon?: IconName;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={`empty${compact ? ' empty--compact' : ''}`} role="status">
      <Icon name={icon} size={compact ? 28 : 44} className="empty__icon" strokeWidth={1.5} />
      <div className="empty__title">{title}</div>
      {children && <div className="empty__body">{children}</div>}
      {action}
    </div>
  );
}

export function ErrorState({ error, what, onRetry, compact }: { error: unknown; what: string; onRetry?: () => void; compact?: boolean }) {
  if (isNotAvailable(error)) {
    const path = safePath((error as ApiError).endpoint);
    return (
      <EmptyState
        icon="plug"
        compact={compact}
        title={`This server doesn't provide ${what} yet`}
        action={
          <Link className="btn btn--sm" to="/settings">
            Connection settings
          </Link>
        }
      >
        The UI reads {what} from <code>{path}</code>, which this varTrack deployment doesn't serve. Check the orchestrator URL in
        Settings.
      </EmptyState>
    );
  }
  return (
    <EmptyState
      icon="alertCircle"
      compact={compact}
      title={`Couldn't load ${what}`}
      action={
        <div className="row">
          {onRetry && (
            <button className="btn btn--sm" onClick={onRetry}>
              <Icon name="refresh" size={14} /> Try again
            </button>
          )}
          <Link className="btn btn--sm btn--ghost" to="/settings">
            Connection settings
          </Link>
        </div>
      }
    >
      {error instanceof Error ? error.message : String(error)}
    </EmptyState>
  );
}

function safePath(endpoint: string): string {
  try {
    return new URL(endpoint, window.location.origin).pathname;
  } catch {
    return endpoint;
  }
}

export function Skeleton({ height = 16, width = '100%', style }: { height?: number; width?: number | string; style?: React.CSSProperties }) {
  return <div className="skeleton" style={{ height, width, ...style }} aria-hidden />;
}

export function LoadingRows({ rows = 4, height = 44 }: { rows?: number; height?: number }) {
  return (
    <div className="stack stack--tight" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} height={height} />
      ))}
    </div>
  );
}

const NOTICE_ICON = { info: 'alertCircle', warning: 'alertTriangle', critical: 'xCircle', good: 'checkCircle' } as const;

export function Notice({ tone = 'info', icon, children }: { tone?: 'info' | 'warning' | 'critical' | 'good'; icon?: IconName; children: ReactNode }) {
  return (
    <div className={`notice notice--${tone}`} role={tone === 'critical' ? 'alert' : undefined}>
      <Icon name={icon ?? NOTICE_ICON[tone]} size={18} style={{ flex: 'none', marginTop: 1 }} />
      <div style={{ minWidth: 0 }}>{children}</div>
    </div>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: ReactNode; title?: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map(o => (
        <button key={o.value} type="button" className="seg__btn" aria-pressed={o.value === value} title={o.title} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A plain card with an optional title row. */
export function Card({ title, actions, children, flush }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; flush?: boolean }) {
  return (
    <section className={`card${flush ? ' card--flush' : ''}`}>
      {title && (
        <header className="row" style={{ marginBottom: flush ? 0 : 16, padding: flush ? '16px 24px' : undefined }}>
          <h2 className="card__title">{title}</h2>
          {actions && <div className="row" style={{ marginLeft: 'auto' }}>{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}
