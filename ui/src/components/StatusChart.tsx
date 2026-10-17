import { useState } from 'react';
import type { Tone } from '../lib/status';
import { TONE_COLOR } from './Status';

export interface ChartSegment {
  key: string;
  label: string;
  tone: Tone;
  count: number;
}

/**
 * Thin proportional bar with counted labels beside it. Clicking a segment or
 * a label filters by that status; the labels keep it readable without color.
 */
export function StatusChart({ segments, unit, onSelect }: { segments: ChartSegment[]; unit: string; onSelect?: (key: string) => void }) {
  const [tip, setTip] = useState<{ x: number; y: number; text: string } | null>(null);
  const total = segments.reduce((n, s) => n + s.count, 0);
  const visible = segments.filter(s => s.count > 0);
  if (total === 0) return null;

  return (
    <div className="status-chart">
      <div className="status-chart__track" role="img" aria-label={`${total} ${unit}: ${visible.map(s => `${s.count} ${s.label.toLowerCase()}`).join(', ')}`}>
        {visible.map(s => {
          const text = `${s.label}: ${s.count} (${Math.round((s.count / total) * 100)}%)`;
          return (
            <button
              key={s.key}
              type="button"
              className={`status-chart__seg bg-${TONE_COLOR[s.tone]}`}
              style={{ flexGrow: s.count, flexBasis: 0 }}
              aria-label={text}
              onClick={() => onSelect?.(s.key)}
              onMouseMove={e => setTip({ x: e.clientX + 12, y: e.clientY + 14, text })}
              onMouseLeave={() => setTip(null)}
              onBlur={() => setTip(null)}
            />
          );
        })}
      </div>
      <div className="status-chart__legend">
        {visible.map(s => (
          <button key={s.key} type="button" onClick={() => onSelect?.(s.key)}>
            <span className={`dot bg-${TONE_COLOR[s.tone]}`} style={{ width: 8, height: 8 }} />
            {s.label} <strong className="num">{s.count}</strong>
          </button>
        ))}
      </div>
      {tip && (
        <div className="tip" style={{ left: tip.x, top: tip.y }} role="tooltip">
          {tip.text}
        </div>
      )}
    </div>
  );
}
