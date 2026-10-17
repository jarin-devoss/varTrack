import { Link, useNavigate } from 'react-router-dom';
import { useApi } from '../api';
import { Segmented, Timestamp } from '../components/common';
import { Hero } from '../components/Hero';
import { Icon } from '../components/Icon';
import { Panel } from '../components/Panel';
import { usePageTitle } from '../layout/page';
import { appPath, platformLabel } from '../lib/platforms';
import { usePrefs, type Theme } from '../lib/prefs';
import { tokenIdentity } from '../lib/token';
import { useQuery } from '../lib/useQuery';

export function ProfilePage() {
  usePageTitle('Profile');
  const [prefs, setPrefs] = usePrefs();
  const navigate = useNavigate();
  const api = useApi();
  const session = useQuery('auth:session', a => a.getSession(), { poll: false });
  const identity = tokenIdentity(prefs.connection.token);
  const signedIn = !!prefs.connection.token;
  const sso = session.data?.enabled ? session.data : null;
  const user = sso?.authenticated ? sso.user : undefined;
  const primary = sso?.platforms.find(p => p.platform === sso.primary);

  return (
    <div className="page-stack">
      <Hero
        color={user || signedIn ? 'green' : 'gray'}
        icon="user"
        kind="Profile"
        name={user?.name ?? identity?.name ?? (signedIn ? 'Signed in' : 'Not signed in')}
        meta={user ? `@${user.login} on ${primary ? platformLabel(primary) : sso?.primary}` : identity?.email ?? (signedIn ? 'Token without readable claims' : `Tenant ${prefs.connection.tenant || 'default'}`)}
      />

      {user && sso && (
        <Panel title="Git platforms">
          <div className="stack">
            {sso.platforms.map(p => (
              <div key={p.platform} className="row row--wrap" style={{ gap: 12, alignItems: 'flex-start' }}>
                <div style={{ minWidth: 180 }}>
                  <div style={{ fontWeight: 500 }}>
                    {platformLabel(p)}
                    {p.platform === sso.primary && <span className="tag" style={{ marginLeft: 8 }}>signed in with</span>}
                  </div>
                  <div className="muted mono" style={{ fontSize: 12 }}>{(p.providerUrl ?? '').replace(/^https?:\/\//, '')}</div>
                </div>
                {p.connected && p.user ? (
                  <dl className="facts" style={{ flex: 1 }}>
                    <div>
                      <dt>Login</dt>
                      <dd className="mono">{p.user.login}</dd>
                    </div>
                    <div>
                      <dt>Email</dt>
                      <dd>{p.user.email || '—'}</dd>
                    </div>
                  </dl>
                ) : (
                  <div className="stack" style={{ flex: 1, gap: 6 }}>
                    <span className="muted" style={{ fontSize: 13 }}>
                      Not connected: its {p.rules} rule{p.rules === 1 ? ' is' : 's are'} hidden until you sign in to {platformLabel(p)} too.
                    </span>
                    <span>
                      <a className="btn btn--sm" href={api.loginUrl(p.platform, appPath('/profile'))}>
                        <Icon name="gitBranch" size={14} /> Connect {platformLabel(p)}
                      </a>
                    </span>
                  </div>
                )}
              </div>
            ))}
            <p className="muted" style={{ fontSize: 13 }}>
              {sso.admin
                ? 'You are a varTrack administrator: you see every rule.'
                : 'Each rule is shown when its own platform lets your account there read one of its repositories, and can be synced when it lets you write. Platforms are asked again every couple of minutes.'}
              {sso.startedAt && (
                <>
                  {' '}
                  Signed in <Timestamp iso={sso.startedAt} />.
                </>
              )}
            </p>
            <div>
              <button
                className="btn"
                onClick={async () => {
                  await api.logout();
                  session.reload();
                  navigate('/login');
                }}
              >
                <Icon name="arrowLeft" size={14} /> Sign out of all platforms
              </button>
            </div>
          </div>
        </Panel>
      )}

      <Panel title={sso ? 'API token' : 'Identity'}>
        {signedIn ? (
          <div className="stack">
            <dl className="facts">
              <div>
                <dt>Username</dt>
                <dd>{identity?.name ?? '—'}</dd>
              </div>
              <div>
                <dt>Email</dt>
                <dd>{identity?.email ?? '—'}</dd>
              </div>
              <div>
                <dt>Issuer</dt>
                <dd className="mono">{identity?.issuer ?? '—'}</dd>
              </div>
              <div>
                <dt>Groups</dt>
                <dd>{identity?.groups.length ? identity.groups.join(', ') : '—'}</dd>
              </div>
              <div>
                <dt>Token expires</dt>
                <dd>{identity?.expiresAt ? <Timestamp iso={identity.expiresAt} /> : '—'}</dd>
              </div>
            </dl>
            <p className="muted" style={{ fontSize: 13 }}>
              Read from the token for display only; the gateway checks it and your role on every request.
            </p>
            <div>
              <button
                className="btn"
                onClick={() => {
                  setPrefs(p => ({ connection: { ...p.connection, token: '' } }));
                  navigate('/login');
                }}
              >
                <Icon name="arrowLeft" size={14} /> Log out
              </button>
            </div>
          </div>
        ) : (
          <div className="stack">
            <p className="secondary">You're not signed in. Reading rules works without a token; syncing and validating through the gateway need one.</p>
            <div>
              <Link className="btn btn--primary" to="/login">
                Sign in
              </Link>
            </div>
          </div>
        )}
      </Panel>

      <Panel title="Preferences">
        <div className="row row--wrap" style={{ gap: 16 }}>
          <span className="field__label" style={{ width: 120 }}>Theme</span>
          <Segmented<Theme>
            label="Theme"
            value={prefs.theme}
            onChange={theme => setPrefs({ theme })}
            options={[
              { value: 'system', label: 'System' },
              { value: 'light', label: 'Light' },
              { value: 'dark', label: 'Dark' },
            ]}
          />
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 12 }}>
          {prefs.pinned.length} favorite rule{prefs.pinned.length === 1 ? '' : 's'} · live updates {prefs.live ? 'on' : 'paused'}
        </p>
      </Panel>
    </div>
  );
}
