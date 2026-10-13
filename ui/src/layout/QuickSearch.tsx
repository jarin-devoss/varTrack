import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { RuleView } from '../api/types';
import { Icon, SINK_ICON, type IconName } from '../components/Icon';
import { DriftIcon } from '../components/Status';
import { useHotkeys } from '../lib/hotkeys';

interface Result {
  group: 'Rules' | 'Environments' | 'Pages';
  label: string;
  hint?: string;
  to: string;
  icon: IconName | 'drift';
  rule?: RuleView;
  env?: string;
}

const PAGES: Result[] = [
  { group: 'Pages', label: 'Home', to: '/home', icon: 'overview' },
  { group: 'Pages', label: 'Favorites', to: '/favorites', icon: 'star' },
  { group: 'Pages', label: 'Rules', to: '/rules', icon: 'rules' },
  { group: 'Pages', label: 'Environments', to: '/environments', icon: 'layers' },
  { group: 'Pages', label: 'Events', to: '/events', icon: 'activity' },
  { group: 'Pages', label: 'Tasks', to: '/tasks', icon: 'tasks' },
  { group: 'Pages', label: 'Playground', to: '/playground', icon: 'playground' },
  { group: 'Pages', label: 'Settings', to: '/settings', icon: 'settings' },
  { group: 'Pages', label: 'Profile', to: '/profile', icon: 'user' },
];

const ruleHrefOf = (name: string, query = '') => `/rules/${name.split('/').map(encodeURIComponent).join('/')}${query}`;

/** "Search" button that opens an inline search over rules, environments and pages. */
export function QuickSearch({ rules, open, onOpenChange }: { rules: RuleView[]; open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const root = useRef<HTMLDivElement>(null);

  useHotkeys({ '/': () => onOpenChange(true) }, !open);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        onOpenChange(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onOpenChange]);
  useEffect(() => {
    if (open) {
      setQ('');
      setActive(0);
      setTimeout(() => input.current?.focus(), 0);
    }
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) onOpenChange(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open, onOpenChange]);

  const results = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const match = (s: string) => !needle || s.toLowerCase().includes(needle);
    const ruleResults: Result[] = rules
      .filter(r => match(`${r.name} ${r.repositories.join(' ')} ${r.fileName ?? ''}`))
      .map(r => ({ group: 'Rules', label: r.name, hint: r.repositories.join(', '), to: ruleHrefOf(r.name), icon: 'drift', rule: r }));
    const envResults: Result[] = rules.flatMap(r =>
      r.targets
        .filter(t => needle && match(`${r.name} ${t.env} ${t.destination}`))
        .map(t => ({ group: 'Environments' as const, label: `${r.name} · ${t.env}`, hint: t.destination, to: ruleHrefOf(r.name, `?env=${encodeURIComponent(t.env)}`), icon: 'drift' as const, rule: r, env: t.env })),
    );
    const pageResults = PAGES.filter(p => needle && match(p.label));
    return [...ruleResults.slice(0, 8), ...envResults.slice(0, 8), ...pageResults];
  }, [q, rules]);

  const go = (r: Result | undefined) => {
    if (!r) return;
    onOpenChange(false);
    navigate(r.to);
  };

  if (!open) {
    return (
      <>
        <button type="button" className="search-trigger hide-mobile" onClick={() => onOpenChange(true)} aria-label="Search (press /)">
          <Icon name="search" size={15} />
          <span>Search</span>
          <kbd>/</kbd>
        </button>
        <button type="button" className="icon-btn show-mobile" onClick={() => onOpenChange(true)} aria-label="Search">
          <Icon name="search" size={16} />
        </button>
      </>
    );
  }

  let lastGroup = '';
  return (
    <div className="qsearch" ref={root}>
      <div className="qsearch__box">
        <Icon name="search" size={15} className="muted" />
        <input
          ref={input}
          className="qsearch__input"
          value={q}
          placeholder="Search rules, environments, pages…"
          aria-label="Search"
          role="combobox"
          aria-expanded="true"
          aria-controls="qsearch-results"
          onChange={e => {
            setQ(e.target.value);
            setActive(0);
          }}
          onKeyDown={e => {
            if (e.key === 'Escape') onOpenChange(false);
            else if (e.key === 'ArrowDown') {
              e.preventDefault();
              setActive(a => Math.min(results.length - 1, a + 1));
            } else if (e.key === 'ArrowUp') {
              e.preventDefault();
              setActive(a => Math.max(0, a - 1));
            } else if (e.key === 'Enter') go(results[active]);
          }}
        />
        <button type="button" className="icon-btn icon-btn--plain" style={{ width: 24, height: 24 }} aria-label="Close search" onClick={() => onOpenChange(false)}>
          <Icon name="x" size={14} />
        </button>
      </div>
      <div className="qsearch__results" id="qsearch-results" role="listbox">
        {results.length === 0 && <div className="qsearch__empty">Nothing matches “{q}”.</div>}
        {results.map((r, i) => {
          const header = r.group !== lastGroup ? <div className="qsearch__group">{r.group}</div> : null;
          lastGroup = r.group;
          const target = r.rule && r.env ? r.rule.targets.find(t => t.env === r.env) : undefined;
          return (
            <div key={`${r.group}:${r.to}`}>
              {header}
              <button
                type="button"
                role="option"
                aria-selected={i === active}
                className="qsearch__item"
                onMouseEnter={() => setActive(i)}
                onClick={() => go(r)}
              >
                {r.icon === 'drift' && r.rule ? (
                  <DriftIcon state={target?.drift ?? worst(r.rule)} size={15} />
                ) : (
                  <Icon name={r.icon as IconName} size={15} className="muted" />
                )}
                {r.rule && !r.env && <Icon name={SINK_ICON[r.rule.sinkType]} size={14} className="faint" />}
                <span className="truncate">{r.label}</span>
                {r.hint && <span className="faint truncate" style={{ marginLeft: 'auto', fontSize: 12 }}>{r.hint}</span>}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function worst(rule: RuleView) {
  const order = ['heal_failed', 'drifted', 'unknown', 'healing', 'in_sync', 'not_watched'] as const;
  return order.find(s => rule.targets.some(t => t.drift === s)) ?? 'not_watched';
}
