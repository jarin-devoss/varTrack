import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Icon } from './Icon';

/** "prefix/name" with a muted prefix and a bold name; a link when `to` is set. */
export function NameLabel({ prefix, name, to }: { prefix?: string; name: string; to?: string }) {
  const content = (
    <>
      {prefix && <span className="rowx__prefix">{prefix}/</span>}
      <strong>{name}</strong>
    </>
  );
  return to ? (
    <Link to={to} className="rowx__name" onClick={e => e.stopPropagation()} title={prefix ? `${prefix}/${name}` : name}>
      {content}
    </Link>
  ) : (
    <span className="rowx__name">{content}</span>
  );
}

export function Star({ active, onToggle, label }: { active: boolean; onToggle: () => void; label: string }) {
  return (
    <button
      type="button"
      className={`star${active ? ' star--on' : ''}`}
      aria-pressed={active}
      aria-label={active ? `Remove ${label} from favorites` : `Add ${label} to favorites`}
      title={active ? 'Remove from favorites' : 'Add to favorites'}
      onClick={e => {
        e.preventDefault();
        e.stopPropagation();
        onToggle();
      }}
    >
      <Icon name="star" size={16} fill={active ? 'currentColor' : 'none'} />
    </button>
  );
}

/**
 * A two-line list row: icon, title and status pills on the first line, a
 * quiet detail line under the title. The chevron opens details underneath.
 */
export function ExpandRow({
  icon,
  title,
  status,
  meta,
  details,
  defaultOpen = false,
  label,
}: {
  icon: ReactNode;
  title: ReactNode;
  status?: ReactNode;
  meta?: ReactNode;
  details: () => ReactNode;
  defaultOpen?: boolean;
  label: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const ref = useRef<HTMLDivElement>(null);
  // A row opened by a deep link scrolls into view once.
  useEffect(() => {
    if (defaultOpen) ref.current?.scrollIntoView({ block: 'center' });
  }, []);
  return (
    <div className="rowx" ref={ref}>
      <div className="rowx__line">
        <span className="rowx__icon">{icon}</span>
        <span className="rowx__title">{title}</span>
        <span className="rowx__status">{status}</span>
        <button type="button" className="rowx__toggle" aria-expanded={open} aria-label={`${open ? 'Hide' : 'Show'} details for ${label}`} title="Details" onClick={() => setOpen(o => !o)}>
          <Icon name="chevronRight" size={14} />
        </button>
        {meta && <span className="rowx__meta">{meta}</span>}
      </div>
      {open && <div className="rowx__details">{details()}</div>}
    </div>
  );
}

/** Shows `step` items at a time with a "Show more" button. */
export function useShowMore<T>(items: T[], step = 50): { visible: T[]; more: ReactNode } {
  const [count, setCount] = useState(step);
  const visible = items.slice(0, count);
  const more =
    items.length > count ? (
      <div className="show-more">
        <button className="btn btn--primary btn--sm" onClick={() => setCount(c => c + step)}>
          Show more ({items.length - count} left)
        </button>
      </div>
    ) : null;
  return { visible, more };
}
