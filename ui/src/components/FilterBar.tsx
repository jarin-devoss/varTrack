import { useState, type ReactNode } from 'react';
import { Icon } from './Icon';

/**
 * Sticky card with the list's filters and its status chart. On phones the
 * filters fold behind a toggle next to the result count.
 */
export function FilterBar({ count, label, loading, children, chart }: { count: number; label: string; loading?: boolean; children: ReactNode; chart?: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`card filter-bar${open ? '' : ' filter-bar--closed'}`}>
      <div className="filter-toggle row" style={{ justifyContent: 'space-between' }}>
        <span className="muted" style={{ fontSize: 13 }}>
          {loading ? 'Loading…' : `${count} ${count === 1 ? label.replace(/s$/, '') : label}`}
        </span>
        <button type="button" className="btn btn--ghost btn--sm" aria-expanded={open} onClick={() => setOpen(o => !o)}>
          <Icon name="filter" size={14} /> Filters
        </button>
      </div>
      <div className="filter-form">{children}</div>
      {chart}
    </div>
  );
}
