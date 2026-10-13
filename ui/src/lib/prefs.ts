import { useCallback, useSyncExternalStore } from 'react';

export type Theme = 'system' | 'light' | 'dark';

export interface ConnectionPrefs {
  gatewayUrl: string;
  orchestratorUrl: string;
  watcherUrl: string;
  token: string;
  tenant: string;
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
    tenant: 'default',
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
      connection: { ...DEFAULT_PREFS.connection, ...parsed.connection },
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

export function usePinned(): [string[], (name: string) => void] {
  const [prefs, set] = usePrefs();
  const toggle = useCallback(
    (name: string) =>
      set(p => ({ pinned: p.pinned.includes(name) ? p.pinned.filter(n => n !== name) : [...p.pinned, name] })),
    [set],
  );
  return [prefs.pinned, toggle];
}
