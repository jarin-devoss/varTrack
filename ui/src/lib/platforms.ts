import type { AuthPlatform } from '../api/types';

const PROVIDER_LABEL = { github: 'GitHub', gitea: 'Gitea' } as const;

/** "Gitea", or "GitHub (github-enterprise)" when the bundle name says more than the kind. */
export function platformLabel(p: Pick<AuthPlatform, 'platform' | 'provider'>): string {
  const kind = PROVIDER_LABEL[p.provider] ?? p.provider;
  return p.platform.toLowerCase() === p.provider ? kind : `${kind} (${p.platform})`;
}

/** The current UI path with the app's base path, for returning after sign-in. */
export function appPath(routerPath: string): string {
  return import.meta.env.BASE_URL.replace(/\/$/, '') + routerPath;
}
