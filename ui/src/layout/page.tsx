import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

export interface SectionCount {
  path: string;
  count?: number;
  label: string;
  loading?: boolean;
}

const CountContext = createContext<{ current: SectionCount | null; set: (c: SectionCount | null) => void }>({
  current: null,
  set: () => {},
});

export function SectionCountProvider({ children }: { children: ReactNode }) {
  const [current, set] = useState<SectionCount | null>(null);
  return <CountContext.Provider value={{ current, set }}>{children}</CountContext.Provider>;
}

export function useSectionCountValue(): SectionCount | null {
  return useContext(CountContext).current;
}

/** Shows "N label" next to the section tabs while this page is open. */
export function useSectionCount(path: string, count: number | undefined, label: string, loading = false) {
  const { set } = useContext(CountContext);
  useEffect(() => {
    set({ path, count, label, loading });
  }, [path, count, label, loading]);
  useEffect(() => () => set(null), []);
}

export function usePageTitle(title: string) {
  useEffect(() => {
    document.title = title ? `${title} · varTrack` : 'varTrack';
  }, [title]);
}
