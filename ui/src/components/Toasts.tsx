import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';
import { Icon, type IconName } from './Icon';

export interface Toast {
  id: number;
  tone: 'good' | 'critical' | 'progress';
  title: string;
  body?: ReactNode;
}

type Notify = (toast: Omit<Toast, 'id'>) => void;

const ToastContext = createContext<Notify>(() => {});

const ICON: Record<Toast['tone'], IconName> = { good: 'checkCircle', critical: 'xCircle', progress: 'loader' };
const TOAST_COLOR: Record<Toast['tone'], string> = { good: 'green', critical: 'red', progress: 'blue' };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = useCallback((id: number) => setToasts(ts => ts.filter(t => t.id !== id)), []);
  const notify = useCallback<Notify>(
    toast => {
      const id = Date.now() + Math.random();
      setToasts(ts => [...ts.slice(-3), { ...toast, id }]);
      setTimeout(() => dismiss(id), toast.tone === 'critical' ? 9000 : 5000);
    },
    [dismiss],
  );

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
