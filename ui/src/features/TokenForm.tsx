import { useState } from 'react';
import { createLiveApi } from '../api/live';
import { Notice } from '../components/common';
import { usePrefs } from '../lib/prefs';

/** What a person pastes is one token: the one `vt login` stores, a Git platform's personal access token or a JWT from an identity provider. */
export function cleanToken(pasted: string): string | null {
  const value = pasted.trim().replace(/^Bearer\s+/i, '');
  return value && !/\s/.test(value) ? value : null;
}

/**
 * A bearer token for the gateway (the CLI API behind the Playground), kept in memory for this tab.
 * *onSaved* runs once it is saved; the sign-in page uses it to go on to the page the person was headed for.
 */
export function TokenForm({ compact = false, submitLabel, onSaved }: { compact?: boolean; submitLabel?: string; onSaved: () => void }) {
  const [prefs, setPrefs] = usePrefs();
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const value = cleanToken(token);
    if (!value) {
      setError("That isn't one token. Paste the token you give `vt login` (a personal access token from your Git platform), with nothing else around it.");
      return;
    }
    setBusy(true);
    setError(null);
    const connection = { ...prefs.connection, token: value };
    const health = await createLiveApi(connection).getHealth();
    setBusy(false);
    setPrefs({ connection });
    if (health.every(s => s.status === 'down')) {
      setError('Saved, but none of the services answered. Check the URLs in Settings.');
      return;
    }
    setToken('');
    onSaved();
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
          placeholder="Paste the token"
          autoComplete="off"
          spellCheck={false}
          autoFocus={!compact}
        />
        <span className="field__hint">Kept in memory for this tab only — never written to storage.</span>
      </label>
      {error && <Notice tone="critical">{error}</Notice>}
      <button className={compact ? 'btn' : 'btn btn--primary'} type="submit" disabled={busy || !token.trim()}>
        {busy ? 'Saving…' : (submitLabel ?? (compact ? 'Use this token' : 'Sign in'))}
      </button>
    </form>
  );
}
