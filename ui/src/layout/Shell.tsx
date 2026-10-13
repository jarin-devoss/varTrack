import { useEffect, useState, type ReactNode } from 'react';
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom';
import { Icon } from '../components/Icon';
import { Dialog } from '../components/Overlay';
import { useHotkeys } from '../lib/hotkeys';
import { usePrefs } from '../lib/prefs';
import { useQuery } from '../lib/useQuery';
import { useSectionCountValue } from './page';
import { QuickSearch } from './QuickSearch';
import { DOCS_URL, UserMenu } from './UserMenu';

const SECTIONS = [
  { to: '/favorites', label: 'Favorites', hotkey: 'f' },
  { to: '/rules', label: 'Rules', hotkey: 'r' },
  { to: '/environments', label: 'Environments', hotkey: 'e' },
  { to: '/events', label: 'Events', hotkey: 'v' },
  { to: '/tasks', label: 'Tasks', hotkey: 't' },
];

const REPO_URL = 'https://github.com/jarin-devoss/varTrack';

export function Shell({ children }: { children: ReactNode }) {
  const [prefs, setPrefs] = usePrefs();
  const [searchOpen, setSearchOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const count = useSectionCountValue();

  // Same keys as the pages: one request serves the header and the page.
  const rules = useQuery('rules', a => a.listRules(), { poll: 10000 });
  const health = useQuery('health', a => a.getHealth(), { poll: 15000 });

  useEffect(() => {
    const root = document.documentElement;
    if (prefs.theme === 'system') delete root.dataset.theme;
    else root.dataset.theme = prefs.theme;
  }, [prefs.theme]);
  useEffect(() => setSearchOpen(false), [location.pathname]);

  const bindings: Record<string, () => void> = {
    '?': () => setShortcutsOpen(true),
    l: () => setPrefs(p => ({ live: !p.live })),
    'g d': () => navigate('/'),
    'g p': () => navigate('/playground'),
    'g s': () => navigate('/settings'),
  };
  for (const s of SECTIONS) bindings[`g ${s.hotkey}`] = () => navigate(s.to);
  useHotkeys(bindings);

  const allDown = !!health.data && health.data.every(s => s.status === 'down');
  const showCount = count && location.pathname === count.path;

  return (
    <div className="app">
      {(allDown || (health.loading && !health.data)) && (
        <div className={`conn-bar ${allDown ? 'conn-bar--down' : 'conn-bar--loading'}`} role={allDown ? 'alert' : undefined} />
      )}
      {allDown && (
        <div className="conn-banner">Can't reach the varTrack services — retrying every 15 seconds. Check the URLs in <Link to="/settings">Settings</Link>.</div>
      )}

      <div className="chrome">
        <header className="header">
          <div className="container header__row">
            {!searchOpen && (
              <Link to="/" className="brand" aria-label="varTrack dashboard" title="Dashboard">
                <span className="brand__title">varTrack</span>
              </Link>
            )}
            {searchOpen && (
              <Link to="/" className="brand hide-mobile" aria-label="varTrack dashboard">
                <span className="brand__title">varTrack</span>
              </Link>
            )}
            {searchOpen && <QuickSearch rules={rules.data ?? []} open onOpenChange={setSearchOpen} />}
            <div className="header__actions">
              {!searchOpen && <QuickSearch rules={rules.data ?? []} open={false} onOpenChange={setSearchOpen} />}
              <span className="header__sep hide-mobile" />
              <button
                type="button"
                className="icon-btn"
                onClick={() => setPrefs(p => ({ live: !p.live }))}
                title={prefs.live ? 'Live updates on — pages refresh every few seconds (L)' : 'Live updates paused (L)'}
                aria-label={prefs.live ? 'Pause live updates' : 'Resume live updates'}
                aria-pressed={prefs.live}
              >
                <Icon name={prefs.live ? 'pulse' : 'pauseCircle'} size={16} className={prefs.live ? 'c-green' : undefined} />
              </button>
              <Link to="/playground" className="icon-btn" title="Playground — validate or dry-run a config file" aria-label="Playground">
                <Icon name="playground" size={16} />
              </Link>
              <UserMenu onShortcuts={() => setShortcutsOpen(true)} />
            </div>
          </div>
        </header>
        <nav className="container" aria-label="Sections">
          <div className="section-nav__row">
            <div className="section-tabs">
              {SECTIONS.map(s => (
                <NavLink key={s.to} to={s.to} className={({ isActive }) => `section-tab${isActive ? ' section-tab--active' : ''}`}>
                  {s.label}
                </NavLink>
              ))}
            </div>
            {showCount && (
              <div className="section-count" aria-live="polite">
                {count.loading ? (
                  <>
                    <Icon name="loader" size={15} className="spin" /> Loading…
                  </>
                ) : (
                  count.count !== undefined && (
                    <span className="num">
                      {count.count} {count.count === 1 ? count.label.replace(/s$/, '') : count.label}
                    </span>
                  )
                )}
              </div>
            )}
          </div>
        </nav>
      </div>

      <main className="main" id="content">
        <div className="container">{children}</div>
      </main>

      <footer className="footer">
        <div className="container footer__row">
          <span>varTrack UI 0.1.0</span>
          <span className="spacer" />
          <a href={REPO_URL} target="_blank" rel="noreferrer">
            <Icon name="external" size={13} /> Source code
          </a>
          <a href={DOCS_URL} target="_blank" rel="noreferrer">
            <Icon name="file" size={13} /> Documentation
          </a>
          <span>Apache-2.0</span>
        </div>
      </footer>

      <ShortcutsDialog open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
    </div>
  );
}

const SHORTCUTS: [string[], string][] = [
  [['/'], 'Search rules, environments and pages'],
  [['Ctrl', 'K'], 'Search, from anywhere'],
  [['?'], 'Show keyboard shortcuts'],
  [['g', 'd'], 'Go to the dashboard'],
  [['g', 'f'], 'Go to Favorites'],
  [['g', 'r'], 'Go to Rules'],
  [['g', 'e'], 'Go to Environments'],
  [['g', 'v'], 'Go to Events'],
  [['g', 't'], 'Go to Tasks'],
  [['g', 'p'], 'Go to the Playground'],
  [['g', 's'], 'Go to Settings'],
  [['l'], 'Pause or resume live updates'],
  [['Esc'], 'Close search, a dialog or a panel'],
];

function ShortcutsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Keyboard shortcuts"
      footer={
        <button className="btn" onClick={onClose} data-autofocus>
          Close
        </button>
      }
    >
      <dl className="shortcuts">
        {SHORTCUTS.map(([keys, text]) => (
          <div key={text} style={{ display: 'contents' }}>
            <dt>
              {keys.map((k, i) => (
                <span key={i} className="row" style={{ gap: 4 }}>
                  {i > 0 && <span className="muted" style={{ fontSize: 12 }}>{keys[0] === 'Ctrl' ? '+' : 'then'}</span>}
                  <kbd>{k}</kbd>
                </span>
              ))}
            </dt>
            <dd>{text}</dd>
          </div>
        ))}
      </dl>
    </Dialog>
  );
}
