import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './Icon';

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Open modals, innermost last. Only the top one reacts to Esc and Tab.
const stack: object[] = [];

/** Esc to close, focus moves in on open and back on close, Tab stays inside. */
function useModal(open: boolean, onClose: () => void) {
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const token = {};
    stack.push(token);
    const previous = document.activeElement as HTMLElement | null;
    const el = panel.current;
    const first = el?.querySelector<HTMLElement>('[data-autofocus]') ?? el?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (stack[stack.length - 1] !== token) return;
      if (e.key === 'Escape') {
        e.stopPropagation();
        closeRef.current();
      } else if (e.key === 'Tab' && el) {
        const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE));
        if (items.length === 0) return;
        const [head, tail] = [items[0], items[items.length - 1]];
        if (e.shiftKey && document.activeElement === head) {
          e.preventDefault();
          tail.focus();
        } else if (!e.shiftKey && document.activeElement === tail) {
          e.preventDefault();
          head.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey, true);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      stack.splice(stack.indexOf(token), 1);
      document.removeEventListener('keydown', onKey, true);
      document.body.style.overflow = prevOverflow;
      previous?.focus?.();
    };
  }, [open]);

  return panel;
}

interface SlideOverProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  /** Buttons shown in the bar at the top of the panel. */
  actions?: ReactNode;
  wide?: boolean;
}

export function SlideOver({ open, onClose, title, subtitle, children, actions, wide }: SlideOverProps) {
  const panel = useModal(open, onClose);
  const titleId = useId();
  if (!open) return null;
  return createPortal(
    <>
      <div className="overlay" onClick={onClose} />
      <div className={`slideover${wide ? ' slideover--wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panel}>
        <div className="slideover__bar">
          {actions}
          <span className="spacer" />
          <button className="btn btn--ghost btn--icon" onClick={onClose} aria-label="Close">
            <Icon name="x" />
          </button>
        </div>
        <header className="slideover__header">
          <h2 className="slideover__title" id={titleId}>
            {title}
          </h2>
          {subtitle && <div className="secondary" style={{ marginTop: 2 }}>{subtitle}</div>}
        </header>
        <div className="slideover__body">{children}</div>
      </div>
    </>,
    document.body,
  );
}

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}

export function Dialog({ open, onClose, title, children, footer, wide }: DialogProps) {
  const panel = useModal(open, onClose);
  const titleId = useId();
  if (!open) return null;
  return createPortal(
    <>
      <div className="overlay overlay--dialog" onClick={onClose} />
      <div className={`dialog${wide ? ' dialog--wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panel}>
        <div className="dialog__body">
          <h2 className="dialog__title" id={titleId}>
            {title}
          </h2>
          {children}
        </div>
        {footer && <footer className="dialog__footer">{footer}</footer>}
      </div>
    </>,
    document.body,
  );
}
