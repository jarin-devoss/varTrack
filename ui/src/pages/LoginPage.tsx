import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { createLiveApi } from '../api/live';
import { Notice } from '../components/common';
import { usePrefs } from '../lib/prefs';
import { tokenIdentity } from '../lib/token';

/** Sign in with the same bearer token the CLI uses. */
export function LoginPage() {
  const [prefs, setPrefs] = usePrefs();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const returnUrl = params.get('return_url') ?? '/';

  useEffect(() => {
    document.title = 'Sign in · varTrack';
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const value = token.trim().replace(/^Bearer\s+/i, '');
    if (!tokenIdentity(value)) {
      setError("That doesn't look like a JWT. Paste the token from `vt login` or your identity provider.");
      return;
    }
    setBusy(true);
    setError(null);
    const connection = { ...prefs.connection, token: value };
    const health = await createLiveApi(connection).getHealth();
    setBusy(false);
    setPrefs({ connection });
    if (health.every(s => s.status === 'down')) {
      setError('Signed in, but none of the services answered. Check the URLs in Settings.');
      return;
    }
    navigate(returnUrl.startsWith('/') ? returnUrl : '/');
  };

  return (
    <div className="login">
      <div className="login__card">
        <div className="login__brand">
          <h1 style={{ fontSize: 24, fontWeight: 600 }}>Sign in to varTrack</h1>
          <p className="muted">Use the bearer token from your identity provider — the same one <code>vt login</code> stores.</p>
        </div>
        <form className="card stack" onSubmit={submit}>
          <label className="field">
            <span className="field__label">Token</span>
            <textarea
              className="textarea"
              style={{ minHeight: 110 }}
              value={token}
              onChange={e => setToken(e.target.value)}
              placeholder="eyJhbGciOi…"
              autoComplete="off"
              spellCheck={false}
              autoFocus
            />
            <span className="field__hint">Kept in memory for this tab only — never written to storage.</span>
          </label>
          {error && <Notice tone="critical">{error}</Notice>}
          <button className="btn btn--primary" type="submit" disabled={busy || !token.trim()}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
          <div className="row" style={{ justifyContent: 'space-between', fontSize: 13 }}>
            <Link to="/settings">Connection settings</Link>
            <Link to="/">Continue without a token</Link>
          </div>
        </form>
      </div>
    </div>
  );
}
