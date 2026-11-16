import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useApi, type VarTrackApi } from '../api';
import { usePrefs } from './prefs';

export interface QueryState<T> {
  data?: T;
  error?: unknown;
  /** No data yet and a request is in flight. */
  loading: boolean;
  /** Any request in flight, including background refreshes. */
  fetching: boolean;
  updatedAt?: number;
  reload: () => void;
}

export const DEFAULT_POLL_MS = 5000;

/**
 * One cached answer per key, shared by every component that asks for it:
 * the header and a page polling the same endpoint send one request, and a
 * page opened again shows what it had at once while it refreshes.
 */
interface Entry {
  data?: unknown;
  error?: unknown;
  updatedAt?: number;
  inFlight?: Promise<void>;
  listeners: Set<() => void>;
  /** Counts every change of the entry (data, error, a request starting or ending): what useSyncExternalStore compares. */
  version: number;
}

// Per API instance, so changing the connection or tenant starts from nothing.
const caches = new WeakMap<VarTrackApi, Map<string, Entry>>();

function entryFor(api: VarTrackApi, key: string): Entry {
  let cache = caches.get(api);
  if (!cache) caches.set(api, (cache = new Map()));
  let entry = cache.get(key);
  if (!entry) cache.set(key, (entry = { listeners: new Set(), version: 0 }));
  return entry;
}

/** The cached answer for *key*, if any, without fetching. */
export function peekQuery<T>(api: VarTrackApi, key: string): T | undefined {
  return caches.get(api)?.get(key)?.data as T | undefined;
}

function notify(entry: Entry) {
  entry.version += 1;
  for (const listener of entry.listeners) listener();
}

function accept(entry: Entry, data: unknown) {
  entry.data = data;
  entry.error = undefined;
  entry.updatedAt = Date.now();
}

/**
 * Put an answer in the cache as if a request had returned it, and tell whoever is subscribed. The same steps a request's
 * answer takes, at a moment the caller picks: a test uses it to land an answer between a component's render and its
 * subscription, which promises alone cannot place reliably.
 */
export function setQueryData<T>(api: VarTrackApi, key: string, data: T): void {
  const entry = entryFor(api, key);
  accept(entry, data);
  notify(entry);
}

/** Fetch unless a request is already in flight or the answer is younger than *maxAgeMs*. */
function revalidate(api: VarTrackApi, entry: Entry, fetcher: (api: VarTrackApi) => Promise<unknown>, maxAgeMs: number) {
  if (entry.inFlight) return;
  if (entry.updatedAt !== undefined && Date.now() - entry.updatedAt < maxAgeMs) return;
  entry.inFlight = fetcher(api).then(
    data => accept(entry, data),
    error => {
      // Keep the last good data; screens show the error only when there is none.
      entry.error = error;
    },
  );
  void entry.inFlight.finally(() => {
    entry.inFlight = undefined;
    notify(entry);
  });
  notify(entry);
}

/**
 * Fetch with stale-while-revalidate polling. Polls only while live updates
 * are on and the tab is visible; keeps the last good data on refresh errors.
 * Pass `null` as key to skip fetching. `placeholder` is shown until the first
 * answer arrives (e.g. the rule from the rules list while the rule loads).
 */
export function useQuery<T>(
  key: string | null,
  fetcher: (api: VarTrackApi) => Promise<T>,
  opts: { poll?: number | false; placeholder?: () => T | undefined } = {},
): QueryState<T> {
  const api = useApi();
  const [prefs] = usePrefs();
  const entry = key === null ? undefined : entryFor(api, key);

  // Latest fetcher without re-subscribing on every render.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // The entry is read at render and subscribed to afterwards. useSyncExternalStore reads its version again once it has
  // subscribed, so an answer that lands between the two re-renders this component: with a plain listener added in an
  // effect, that answer reached nobody, and a component that never asks again (poll: false) kept the empty state.
  const subscribe = useCallback(
    (listener: () => void) => {
      if (!entry) return () => {};
      entry.listeners.add(listener);
      return () => {
        entry.listeners.delete(listener);
      };
    },
    [entry],
  );
  useSyncExternalStore(subscribe, () => entry?.version ?? 0);

  useEffect(() => {
    if (!entry) return;
    // Opened (again): refresh unless another component just did.
    revalidate(api, entry, a => fetcherRef.current(a), 1000);
  }, [api, entry]);

  const interval = opts.poll === false || !prefs.live ? 0 : (opts.poll ?? DEFAULT_POLL_MS);
  useEffect(() => {
    if (!interval || !entry) return;
    const timer = setInterval(() => {
      // Half the interval: a second component polling the same key doesn't double the requests.
      if (document.visibilityState === 'visible') revalidate(api, entry, a => fetcherRef.current(a), interval / 2);
    }, interval);
    return () => clearInterval(timer);
  }, [api, entry, interval]);

  const reload = useCallback(() => {
    if (entry) revalidate(api, entry, a => fetcherRef.current(a), 0);
  }, [api, entry]);

  if (!entry) return { loading: false, fetching: false, reload };
  const data = (entry.data as T | undefined) ?? opts.placeholder?.();
  return {
    data,
    error: entry.error,
    loading: data === undefined && entry.error === undefined,
    fetching: entry.inFlight !== undefined,
    updatedAt: entry.updatedAt,
    reload,
  };
}

/** Re-render on an interval so relative timestamps stay current. */
export function useNow(intervalMs = 15000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}
