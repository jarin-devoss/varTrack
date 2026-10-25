import { useCallback, useSyncExternalStore } from 'react';
import { isPinned, ruleKey } from './rules';

export type Theme = 'system' | 'light' | 'dark';

export interface ConnectionPrefs {
  gatewayUrl: string;
  orchestratorUrl: string;
  watcherUrl: string;
  token: string;
}

export interface Prefs {
  theme: Theme;
  pinned: string[];
  /** Poll for fresh data while a page is open. */
  live: boolean;
  /** Diff shows changed keys only (vs. every key as context). */
  diffCompact: boolean;
  /** Diff as one column with -/+ lines (vs. current and desired side by side). */
  diffUnified: boolean;
  connection: ConnectionPrefs;
}

export const DEFAULT_PREFS: Prefs = {
  theme: 'system',
  pinned: [],
  live: true,
  diffCompact: true,
  diffUnified: false,
  connection: {
    gatewayUrl: '/api/gateway',
    orchestratorUrl: '/api/orchestrator',
    watcherUrl: '/api/watcher',
    token: '',
  },
};

const STORAGE_KEY = 'vartrack.prefs.v1';

function load(): Prefs {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_PREFS;
    const parsed = JSON.parse(raw) as Partial<Prefs>;
    return {
      ...DEFAULT_PREFS,
      ...parsed,
      // A tenant saved by an earlier version is dropped: the tenants come from what Git lets the user see.
      connection: (({ tenant: _tenant, ...rest }) => ({ ...DEFAULT_PREFS.connection, ...rest }))(
        (parsed.connection ?? {}) as Partial<ConnectionPrefs> & { tenant?: string },
      ),
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

let current: Prefs = typeof window === 'undefined' ? DEFAULT_PREFS : load();
const listeners = new Set<() => void>();

function setPrefs(update: Partial<Prefs> | ((p: Prefs) => Partial<Prefs>)) {
  const patch = typeof update === 'function' ? update(current) : update;
  current = { ...current, ...patch };
  try {
    // The bearer token is kept for the session only, never written to disk.
    const persisted = { ...current, connection: { ...current.connection, token: '' } };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
  } catch {
    // Storage can be unavailable (private mode); prefs still work in memory.
  }
  listeners.forEach(l => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function usePrefs(): [Prefs, typeof setPrefs] {
  const prefs = useSyncExternalStore(subscribe, () => current, () => DEFAULT_PREFS);
  return [prefs, setPrefs];
}

/**
 * Favorites, and a toggle for one. Entries are `tenant/platform/datasource`
 * (see lib/rules.ts); toggling a rule off also drops a bare `platform/datasource`
 * entry saved by an earlier version.
 */
export function usePinned(): [string[], (rule: { name: string; tenant?: string }) => void] {
  const [prefs, set] = usePrefs();
  const toggle = useCallback(
    (rule: { name: string; tenant?: string }) =>
      set(p => {
        const key = ruleKey(rule);
        return { pinned: isPinned(p.pinned, rule) ? p.pinned.filter(n => n !== key && n !== rule.name) : [...p.pinned, key] };
      }),
    [set],
  );
  return [prefs.pinned, toggle];
}
