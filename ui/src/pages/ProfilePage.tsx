import { Link, useNavigate } from 'react-router-dom';
import { Segmented, Timestamp } from '../components/common';
import { Hero } from '../components/Hero';
import { Icon } from '../components/Icon';
import { Panel } from '../components/Panel';
import { usePageTitle } from '../layout/page';
import { usePrefs, type Theme } from '../lib/prefs';
import { tokenIdentity } from '../lib/token';

export function ProfilePage() {
  usePageTitle('Profile');
  const [prefs, setPrefs] = usePrefs();
  const navigate = useNavigate();
  const identity = tokenIdentity(prefs.connection.token);
  const signedIn = !!prefs.connection.token;

  return (
    <div className="page-stack">
      <Hero
        color={signedIn ? 'green' : 'gray'}
        icon="user"
        kind="Profile"
        name={identity?.name ?? (signedIn ? 'Signed in' : 'Not signed in')}
        meta={identity?.email ?? (signedIn ? 'Token without readable claims' : `Tenant ${prefs.connection.tenant || 'default'}`)}
      />

      <Panel title="Identity">
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
