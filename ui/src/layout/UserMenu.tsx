import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useApi } from '../api';
import { Icon, type IconName } from '../components/Icon';
import { appPath, platformLabel } from '../lib/platforms';
import { usePrefs, type Theme } from '../lib/prefs';
import { tokenIdentity } from '../lib/token';
import { useQuery } from '../lib/useQuery';

const THEME_NEXT: Record<Theme, Theme> = { system: 'light', light: 'dark', dark: 'system' };
const THEME_ICON: Record<Theme, IconName> = { system: 'monitor', light: 'sun', dark: 'moon' };
const THEME_LABEL: Record<Theme, string> = { system: 'System theme', light: 'Light theme', dark: 'Dark theme' };

export const DOCS_URL = 'https://jarin-devoss.github.io/varTrack/';

export function UserMenu({ onShortcuts }: { onShortcuts: () => void }) {
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [prefs, setPrefs] = usePrefs();
  const navigate = useNavigate();
  const location = useLocation();
  const root = useRef<HTMLDivElement>(null);
  const session = useQuery('auth:session', a => a.getSession(), { poll: 60000 });
  const identity = tokenIdentity(prefs.connection.token);
  // Signed in with the Git platform, or carrying a bearer token for the gateway.
  const user = session.data?.authenticated ? session.data.user : undefined;
  const [avatarFailed, setAvatarFailed] = useState(false);

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

  const name = user?.name ?? identity?.name ?? 'Not signed in';
  const platforms = session.data?.authenticated ? session.data.platforms : [];
  const sub = user
    ? `@${user.login} · ${session.data?.primary ?? ''}`
    : identity
      ? identity.issuer ?? 'signed in'
      : 'not signed in';

  const signOut = async () => {
    setOpen(false);
    if (user) {
      try {
        await api.logout();
      } finally {
        session.reload();
      }
    }
    setPrefs(p => ({ connection: { ...p.connection, token: '' } }));
    navigate('/login');
  };

  // The platform's avatar URL may name a host the browser cannot reach: fall back to the icon.
  const avatar = user?.avatarUrl && !avatarFailed ? (
    <img src={user.avatarUrl} alt="" width={20} height={20} style={{ borderRadius: '50%' }} referrerPolicy="no-referrer" onError={() => setAvatarFailed(true)} />
  ) : (
    <Icon name="user" size={16} />
  );

  return (
    <div className="dropdown-wrap" ref={root}>
      <button type="button" className="icon-btn" aria-haspopup="menu" aria-expanded={open} aria-label="User menu" onClick={() => setOpen(o => !o)}>
        {avatar}
      </button>
      {open && (
        <div className="dropdown" role="menu">
          <div className="dropdown__head">
            <span className="dropdown__avatar">{avatar}</span>
            <span style={{ minWidth: 0 }}>
              <div className="truncate" style={{ fontWeight: 500 }}>{name}</div>
              <div className="muted truncate" style={{ fontSize: 12 }}>{sub}</div>
            </span>
          </div>
          {platforms.length > 1 && (
            <>
              <div className="dropdown__sep" />
              {platforms.map(p =>
                p.connected ? (
                  <div key={p.platform} className="dropdown__item muted" style={{ cursor: 'default' }} title={`Connected to ${platformLabel(p)}`}>
                    <Icon name="check" size={16} /> {platformLabel(p)} · @{p.user?.login}
                  </div>
                ) : (
                  <a
                    key={p.platform}
                    role="menuitem"
                    className="dropdown__item"
                    href={api.loginUrl(p.platform, appPath(location.pathname + location.search))}
                    title={`Sign in to ${platformLabel(p)} as well to see its rules`}
                  >
                    <Icon name="gitBranch" size={16} /> Connect {platformLabel(p)}
                    {(p.rules ?? 0) > 0 && <span className="muted" style={{ marginLeft: 'auto', fontSize: 12 }}>{p.rules} rules</span>}
                  </a>
                ),
              )}
            </>
          )}
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
          <div className="dropdown__sep" />
          {user || prefs.connection.token ? (
            <button role="menuitem" type="button" className="dropdown__item" onClick={() => void signOut()}>
              <Icon name="arrowLeft" size={16} /> Sign out
            </button>
          ) : (
            <Link role="menuitem" className="dropdown__item" to="/login" onClick={() => setOpen(false)}>
              <Icon name="key" size={16} /> Sign in
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
