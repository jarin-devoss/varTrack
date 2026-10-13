export interface Identity {
  name: string;
  email?: string;
  issuer?: string;
  groups: string[];
  expiresAt?: string;
}

/**
 * Reads who a bearer token belongs to, for display only. The token is not
 * verified here — the gateway does that on every request.
 */
export function tokenIdentity(token: string): Identity | null {
  const part = token.split('.')[1];
  if (!part) return null;
  try {
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(part.length / 4) * 4, '='));
    const claims = JSON.parse(json) as Record<string, unknown>;
    const str = (v: unknown) => (typeof v === 'string' ? v : undefined);
    const groups = Array.isArray(claims.groups) ? claims.groups.filter((g): g is string => typeof g === 'string') : [];
    return {
      name: str(claims.preferred_username) ?? str(claims.name) ?? str(claims.email) ?? str(claims.sub) ?? 'unknown',
      email: str(claims.email),
      issuer: str(claims.iss),
      groups,
      expiresAt: typeof claims.exp === 'number' ? new Date(claims.exp * 1000).toISOString() : undefined,
    };
  } catch {
    return null;
  }
}
