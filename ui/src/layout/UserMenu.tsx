import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Icon, type IconName } from '../components/Icon';
import { usePrefs, type Theme } from '../lib/prefs';
import { tokenIdentity } from '../lib/token';

const THEME_NEXT: Record<Theme, Theme> = { system: 'light', light: 'dark', dark: 'system' };
const THEME_ICON: Record<Theme, IconName> = { system: 'monitor', light: 'sun', dark: 'moon' };
const THEME_LABEL: Record<Theme, string> = { system: 'System theme', light: 'Light theme', dark: 'Dark theme' };

export const DOCS_URL = 'https://jarin-devoss.github.io/varTrack/';

export function UserMenu({ onShortcuts }: { onShortcuts: () => void }) {
  const [open, setOpen] = useState(false);
  const [prefs, setPrefs] = usePrefs();
  const navigate = useNavigate();
  const root = useRef<HTMLDivElement>(null);
  const identity = tokenIdentity(prefs.connection.token);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', close);
    };
  }, [open]);

  const name = identity?.name ?? 'Not signed in';
  const sub = identity ? identity.issuer ?? 'signed in' : `tenant ${prefs.connection.tenant || 'default'}`;

  return (
    <div className="dropdown-wrap" ref={root}>
      <button type="button" className="icon-btn" aria-haspopup="menu" aria-expanded={open} aria-label="User menu" onClick={() => setOpen(o => !o)}>
        <Icon name="user" size={16} />
      </button>
      {open && (
        <div className="dropdown" role="menu">
          <div className="dropdown__head">
            <span className="dropdown__avatar">
              <Icon name="user" size={16} />
            </span>
            <span style={{ minWidth: 0 }}>
              <div className="truncate" style={{ fontWeight: 500 }}>{name}</div>
              <div className="muted truncate" style={{ fontSize: 12 }}>{sub}</div>
            </span>
          </div>
          <div className="dropdown__sep" />
          <Link role="menuitem" className="dropdown__item" to="/profile" onClick={() => setOpen(false)}>
            <Icon name="user" size={16} /> Profile
          </Link>
          <button role="menuitem" type="button" className="dropdown__item" onClick={() => setPrefs(p => ({ theme: THEME_NEXT[p.theme] }))}>
            <Icon name={THEME_ICON[prefs.theme]} size={16} /> {THEME_LABEL[prefs.theme]}
          </button>
          <button
            role="menuitem"
            type="button"
            className="dropdown__item"
            onClick={() => {
              setOpen(false);
              onShortcuts();
            }}
          >
            <Icon name="keyboard" size={16} /> Keyboard shortcuts
          </button>
          <Link role="menuitem" className="dropdown__item" to="/settings" onClick={() => setOpen(false)}>
            <Icon name="settings" size={16} /> Settings
          </Link>
          <a role="menuitem" className="dropdown__item" href={DOCS_URL} target="_blank" rel="noreferrer">
            <Icon name="file" size={16} /> Documentation
          </a>
          <>
            <div className="dropdown__sep" />
              {prefs.connection.token ? (
                <button
                  role="menuitem"
                  type="button"
                  className="dropdown__item"
                  onClick={() => {
                    setPrefs(p => ({ connection: { ...p.connection, token: '' } }));
                    setOpen(false);
                    navigate('/login');
                  }}
                >
                  <Icon name="arrowLeft" size={16} /> Log out
                </button>
              ) : (
                <Link role="menuitem" className="dropdown__item" to="/login" onClick={() => setOpen(false)}>
                  <Icon name="key" size={16} /> Sign in
                </Link>
              )}
          </>
        </div>
      )}
    </div>
  );
}
