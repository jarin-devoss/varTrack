import crypto from 'node:crypto';

/**
 * A signed internal grant, the way e2e/init/grant.py mints one (and app/api/tokens.py verifies it): a compact HS256
 * token. The UI itself never holds one (it signs in with a session); the specs use one to do what only the stack's own
 * services may do, such as asking the orchestrator to read the schema repository afresh after a spec has changed it.
 */
const b64 = (raw: Buffer | string): string => Buffer.from(raw).toString('base64url');

export function mintGrant(opts: { audience?: string; subject?: string; platform?: string; datasource?: string; ttl?: number } = {}): string {
  const secret = process.env.INTERNAL_AUTH_SECRET ?? process.env.VARTRACK_DEMO_INTERNAL_AUTH_SECRET ?? 'demo-internal-auth-secret-0123456789abcdef';
  const now = Math.floor(Date.now() / 1000);
  const claims: Record<string, unknown> = {
    iss: 'vartrack',
    aud: opts.audience ?? 'sync',
    iat: now,
    nbf: now,
    exp: now + (opts.ttl ?? 60),
    sub: opts.subject ?? 'ui-e2e',
    platform: opts.platform ?? 'gitea',
    ...(opts.datasource ? { datasource: opts.datasource } : {}),
  };
  const header = b64(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const body = b64(JSON.stringify(claims));
  const signature = crypto.createHmac('sha256', secret).update(`${header}.${body}`).digest('base64url');
  return `${header}.${body}.${signature}`;
}
