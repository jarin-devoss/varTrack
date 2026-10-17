import { useId, useState, type ReactNode } from 'react';
import { Icon } from './Icon';

/** A card with a clickable title row that collapses its content. */
export function Panel({
  title,
  subtitle,
  id,
  defaultExpanded = true,
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  id?: string;
  defaultExpanded?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultExpanded);
  const bodyId = useId();
  return (
    <section className="card panel" id={id} style={{ padding: 0 }}>
      <button type="button" className="panel__header" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(o => !o)}>
        <div style={{ minWidth: 0 }}>
          <h3 className="panel__title">{title}</h3>
          {subtitle && <div className="panel__subtitle">{subtitle}</div>}
        </div>
        <Icon name="chevronDown" size={20} className={`panel__chevron${open ? ' panel__chevron--open' : ''}`} />
      </button>
      {open && (
        <div className="panel__body" id={bodyId}>
          {children}
        </div>
      )}
    </section>
  );
}

export interface UTab<T extends string> {
  id: T;
  label: ReactNode;
  count?: number;
}

/** Underlined tabs; arrow keys move between them. */
export function UTabs<T extends string>({ tabs, active, onChange, label }: { tabs: UTab<T>[]; active: T; onChange: (id: T) => void; label: string }) {
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const i = tabs.findIndex(t => t.id === active);
    const next = e.key === 'ArrowRight' ? (i + 1) % tabs.length : e.key === 'ArrowLeft' ? (i - 1 + tabs.length) % tabs.length : -1;
    if (next < 0) return;
    e.preventDefault();
    onChange(tabs[next].id);
    e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
  };
  return (
    <div className="utabs" role="tablist" aria-label={label} onKeyDown={onKeyDown}>
      {tabs.map(t => (
        <button
          key={t.id}
          type="button"
          role="tab"
          className="utab"
          aria-selected={t.id === active}
          tabIndex={t.id === active ? 0 : -1}
          onClick={() => onChange(t.id)}
        >
          {t.label}
          {t.count !== undefined && <span className="utab__count num">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}
