// @vitest-environment jsdom
/**
 * useQuery's cache entry is shared by every component that asks for a key. A component reads it while it renders and
 * subscribes to it afterwards, so an answer that arrives in between must still reach it.
 *
 * These tests render on a real root outside React's act(). The answer is put in the cache from a layout effect, which runs in
 * the commit: after the component rendered (it saw no data) and before its passive effects (it has not subscribed yet).
 * Releasing a promise there is not enough: how many promise steps fit before React runs its effects is not something to
 * depend on, so the one that failed in the browser would pass here some of the time.
 */
import { useLayoutEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiProvider, type VarTrackApi } from '../api';
import { setQueryData, useQuery, type QueryState } from './useQuery';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;

/** Long enough for React to commit, flush its effects and re-render. */
const settle = () => new Promise(resolve => setTimeout(resolve, 30));

let api: VarTrackApi;
let roots: Root[];
let containers: HTMLElement[];

beforeEach(() => {
  api = {} as VarTrackApi; // each test has its own: the cache is kept per API
  roots = [];
  containers = [];
});

afterEach(() => {
  for (const root of roots) root.unmount();
  for (const container of containers) container.remove();
});

function mount(element: JSX.Element): HTMLElement {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  root.render(<ApiProvider api={api}>{element}</ApiProvider>);
  roots.push(root);
  containers.push(container);
  return container;
}

interface ProbeProps {
  fetcher: () => Promise<string>;
  /** Called in the commit, before React runs this component's effects. */
  onCommit?: () => void;
  latest?: { current?: QueryState<string> };
}

function Probe({ fetcher, onCommit, latest }: ProbeProps) {
  const query = useQuery('probe', fetcher, { poll: false });
  if (latest) latest.current = query;
  useLayoutEffect(() => onCommit?.(), []); // eslint-disable-line react-hooks/exhaustive-deps
  return <p>{query.data ?? (query.error ? 'failed' : 'nothing')}</p>;
}

describe('useQuery', () => {
  it('shows an answer that arrives after the component rendered and before it subscribed', async () => {
    const fetcher = vi.fn(() => new Promise<string>(() => {})); // the request stays out: the answer below is the only one

    const first = mount(<Probe fetcher={fetcher} />);
    await settle();
    expect(first.textContent).toBe('nothing');

    // A second component for the same key, with the answer landing in its commit.
    const second = mount(<Probe fetcher={fetcher} onCommit={() => setQueryData(api, 'probe', 'the answer')} />);
    await settle();

    expect(first.textContent).toBe('the answer');
    expect(second.textContent).toBe('the answer');
  });

  it('sends one request for components that ask for the same key', async () => {
    const fetcher = vi.fn(() => Promise.resolve('one answer'));

    const first = mount(<Probe fetcher={fetcher} />);
    const second = mount(<Probe fetcher={fetcher} />);
    await settle();

    expect(first.textContent).toBe('one answer');
    expect(second.textContent).toBe('one answer');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('keeps the last good answer when a refresh fails, and says the request failed', async () => {
    const fetcher = vi.fn<() => Promise<string>>().mockResolvedValueOnce('good').mockRejectedValueOnce(new Error('down'));
    const latest: { current?: QueryState<string> } = {};

    const container = mount(<Probe fetcher={fetcher} latest={latest} />);
    await settle();
    expect(container.textContent).toBe('good');

    latest.current!.reload();
    await settle();

    expect(container.textContent).toBe('good');
    expect(latest.current!.error).toBeInstanceOf(Error);
  });

  it('shows no data and says so while the first request is still out', async () => {
    const latest: { current?: QueryState<string> } = {};
    mount(<Probe fetcher={() => new Promise<string>(() => {})} latest={latest} />);
    await settle();

    expect(latest.current).toMatchObject({ loading: true, fetching: true, data: undefined });
  });
});
