import { useEffect, useRef } from 'react';

/** A handler may return false to leave the key to the browser. */
export type Bindings = Record<string, (e: KeyboardEvent) => void | boolean>;

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

/**
 * Global keyboard shortcuts. Keys are `KeyboardEvent.key` values; a space
 * separates a two-key sequence ("g r"). Shortcuts are ignored while typing
 * in a field, except Escape.
 */
export function useHotkeys(bindings: Bindings, enabled = true) {
  const ref = useRef(bindings);
  ref.current = bindings;

  useEffect(() => {
    if (!enabled) return;
    let pending: { key: string; at: number } | null = null;

    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key !== 'Escape' && isTyping(e.target)) return;

      if (pending && Date.now() - pending.at < 1200) {
        const seq = `${pending.key} ${e.key}`;
        pending = null;
        const handler = ref.current[seq];
        if (handler) {
          if (handler(e) !== false) e.preventDefault();
          return;
        }
      }
      const startsSequence = Object.keys(ref.current).some(k => k.startsWith(`${e.key} `));
      const handler = ref.current[e.key];
      if (handler) {
        if (handler(e) !== false) e.preventDefault();
      } else if (startsSequence) {
        pending = { key: e.key, at: Date.now() };
      }
    };

    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [enabled]);
}
