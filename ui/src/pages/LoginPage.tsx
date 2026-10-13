import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useApi } from '../api';
import { createLiveApi } from '../api/live';
import { Notice } from '../components/common';
import { Icon } from '../components/Icon';
import { Logo } from '../layout/Logo';
import { appPath, platformLabel } from '../lib/platforms';
import { usePrefs } from '../lib/prefs';
import { tokenIdentity } from '../lib/token';
import { useQuery } from '../lib/useQuery';

/** Why the last sign-in did not work (the orchestrator's callback sends ?error=). */
const LOGIN_ERRORS: Record<string, string> = {
  denied: 'You declined access on the consent page.',
  state: 'That sign-in was started in another browser or tab. Try again from here.',
  expired: 'The sign-in took too long or was already used. Try again.',
  provider: "The Git platform didn't accept the sign-in. Try again, or ask an administrator to check the OAuth application.",
  platform: 'That platform is not set up for sign-in here.',
};

/**
 * Sign in with the Git platform when single sign-on is on: the rules you see
 * are the ones whose repositories the platform lets you read. Without it, a
 * bearer token (the one `vt login` stores) signs requests to the gateway.
 */
export function LoginPage() {
  const api = useApi();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const session = useQuery('auth:session', a => a.getSession(), { poll: false });
  const returnUrl = safeReturnUrl(params.get('return_url'));
  const error = params.get('error');

  useEffect(() => {
    document.title = 'Sign in · varTrack';
  }, []);

  // Already signed in with the Git platform: go where the user was headed.
  useEffect(() => {
    if (session.data?.enabled && session.data.authenticated) navigate(returnUrl, { replace: true });
  }, [session.data, returnUrl, navigate]);

  const sso = session.data?.enabled ? session.data : null;
  const platforms = sso?.platforms ?? [];
  const names = platforms.map(platformLabel);
  const provider = names.length === 1 ? names[0] : 'your Git platform';

  return (
    <div className="login">
      <div className="login__card">
        <div className="login__brand">
          <Logo className="brand__logo" />
          <h1 style={{ fontSize: 24, fontWeight: 600 }}>Sign in to varTrack</h1>
          <p className="muted">
            {sso
              ? `Sign in with ${provider}. You see the rules whose repositories ${provider} lets you read, and sync the ones you can write to.` +
                (names.length > 1 ? ' Signed in with one platform, you can connect the others to see their rules too.' : '')
              : 'Use the bearer token from your identity provider — the same one vt login stores.'}
          </p>
        </div>

        {sso ? (
          <div className="card stack">
            {error && <Notice tone="critical">{LOGIN_ERRORS[error] ?? 'Sign-in failed. Try again.'}</Notice>}
            {sso.error && <Notice tone="critical">Sign-in is misconfigured on the server; the orchestrator's log says why.</Notice>}
            {platforms.map(p => (
              <a
                key={p.platform}
                className="btn"
                href={api.loginUrl(p.platform, appPath(returnUrl))}
                style={{ justifyContent: 'center' }}
              >
                <Icon name="gitBranch" size={16} /> Sign in with {platformLabel(p)}
              </a>
            ))}
          </div>
        ) : (
          <TokenForm returnUrl={returnUrl} />
        )}

        {sso && (
          <details className="card" style={{ marginTop: 12 }}>
            <summary className="muted" style={{ cursor: 'pointer', fontSize: 13 }}>API token for the Playground</summary>
            <div style={{ marginTop: 12 }}>
              <TokenForm returnUrl={returnUrl} compact />
            </div>
          </details>
        )}
      </div>
    </div>
  );
}

/** A bearer token for the gateway (validate / push from the Playground), kept in memory for this tab. */
function TokenForm({ returnUrl, compact = false }: { returnUrl: string; compact?: boolean }) {
  const [prefs, setPrefs] = usePrefs();
  const navigate = useNavigate();
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
    navigate(returnUrl);
  };

  return (
    <form className={compact ? 'stack' : 'card stack'} onSubmit={submit}>
      <label className="field">
        <span className="field__label">Token</span>
        <textarea
          className="textarea"
          style={{ minHeight: compact ? 80 : 110 }}
          value={token}
          onChange={e => setToken(e.target.value)}
          placeholder="eyJhbGciOi…"
          autoComplete="off"
          spellCheck={false}
          autoFocus={!compact}
        />
        <span className="field__hint">Kept in memory for this tab only — never written to storage.</span>
      </label>
      {error && <Notice tone="critical">{error}</Notice>}
      <button className={compact ? 'btn' : 'btn btn--primary'} type="submit" disabled={busy || !token.trim()}>
        {busy ? 'Saving…' : compact ? 'Use this token' : 'Sign in'}
      </button>
      {!compact && (
        <div className="row" style={{ justifyContent: 'space-between', fontSize: 13 }}>
          <Link to="/settings">Connection settings</Link>
          <Link to="/home">Continue without a token</Link>
        </div>
      )}
    </form>
  );
}

function safeReturnUrl(url: string | null): string {
  return url && url.startsWith('/') && !url.startsWith('//') && !url.startsWith('/\\') && !url.startsWith('/login') ? url : '/';
}
