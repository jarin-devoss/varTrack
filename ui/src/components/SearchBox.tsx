import { useRef } from 'react';
import { useHotkeys } from '../lib/hotkeys';

interface Props {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  /** Focus with "/" (only when the page has no other search). */
  hotkey?: boolean;
}

/** Small search field for filter bars; Esc clears, then blurs. */
export function SearchBox({ value, onChange, placeholder, hotkey = false }: Props) {
  const input = useRef<HTMLInputElement>(null);
  useHotkeys({ '/': () => input.current?.focus() }, hotkey);
  return (
    <input
      ref={input}
      className="ffield ffield--grow"
      type="search"
      value={value}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={e => onChange(e.target.value)}
      onKeyDown={e => {
        if (e.key === 'Escape') {
          if (value) onChange('');
          else input.current?.blur();
        }
      }}
    />
  );
}
