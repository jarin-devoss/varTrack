import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { Icon, type IconName } from './Icon';

export interface Toast {
  id: number;
  tone: 'good' | 'critical' | 'progress';
  title: string;
  body?: ReactNode;
  /** Stays until dismissed (a failure, which a person should read), instead of going after a few seconds. */
  sticky?: boolean;
}

export type ToastContent = Omit<Toast, 'id'>;

/** Shows a toast and returns its id; `update` replaces what a toast says (a progress toast turning into its outcome). */
export interface Notify {
  (toast: ToastContent): number;
  update: (id: number, toast: ToastContent) => void;
}

const noop = Object.assign(() => 0, { update: () => {} }) as Notify;
const ToastContext = createContext<Notify>(noop);

const ICON: Record<Toast['tone'], IconName> = { good: 'checkCircle', critical: 'xCircle', progress: 'loader' };
const TOAST_COLOR: Record<Toast['tone'], string> = { good: 'green', critical: 'red', progress: 'blue' };

/** How long a toast stays: a progress toast until its outcome replaces it (a minute at most), a failure until dismissed. */
function lifetime(toast: ToastContent): number | null {
  if (toast.sticky) return null;
  if (toast.tone === 'progress') return 60_000;
  return toast.tone === 'critical' ? 9_000 : 5_000;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef(new Map<number, number>());
  const shown = useRef(new Set<number>());

  const dismiss = useCallback((id: number) => {
    window.clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    shown.current.delete(id);
    setToasts(ts => ts.filter(t => t.id !== id));
  }, []);

  const arm = useCallback(
    (id: number, toast: ToastContent) => {
      window.clearTimeout(timers.current.get(id));
      const ms = lifetime(toast);
      if (ms !== null) timers.current.set(id, window.setTimeout(() => dismiss(id), ms));
      else timers.current.delete(id);
    },
    [dismiss],
  );

  const notify = useMemo<Notify>(() => {
    const show = (toast: ToastContent) => {
      const id = Date.now() + Math.random();
      shown.current.add(id);
      setToasts(ts => [...ts.slice(-3), { ...toast, id }]);
      arm(id, toast);
      return id;
    };
    const update = (id: number, toast: ToastContent) => {
      // A toast the person already dismissed (or that timed out) is not brought back.
      if (!shown.current.has(id)) return;
      setToasts(ts => ts.map(t => (t.id === id ? { ...toast, id } : t)));
      arm(id, toast);
    };
    return Object.assign(show, { update });
  }, [arm]);

  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map(t => (
          <div key={t.id} className="toast" role={t.tone === 'critical' ? 'alert' : 'status'}>
            <Icon name={ICON[t.tone]} size={18} className={`c-${TOAST_COLOR[t.tone]}${t.tone === 'progress' ? ' spin' : ''}`} style={{ flex: 'none', marginTop: 1 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="toast__title">{t.title}</div>
              {t.body && <div className="toast__body">{t.body}</div>}
            </div>
            <button className="btn btn--ghost btn--icon btn--sm" aria-label="Dismiss" onClick={() => dismiss(t.id)}>
              <Icon name="x" size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): Notify {
  return useContext(ToastContext);
}
