import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { formatDateTime } from '../lib/time';
import { Icon, type IconName } from './Icon';

export type HeroColor = 'green' | 'yellow' | 'red' | 'blue' | 'orange' | 'gray';

interface Props {
  color: HeroColor;
  icon: IconName;
  spin?: boolean;
  /** Makes the whole card a link. */
  to?: string;
  lastUpdated?: string;
  /** Structured mode: KIND / name / meta line. */
  kind?: ReactNode;
  name?: ReactNode;
  meta?: ReactNode;
  titleAction?: ReactNode;
  /** Custom mode: anything in the middle. */
  children?: ReactNode;
}

/** The status-tinted card at the top of the dashboard and detail pages. */
export function Hero({ color, icon, spin, to, lastUpdated, kind, name, meta, titleAction, children }: Props) {
  const body = (
    <>
      <div className="hero__disc">
        <Icon name={icon} size={36} strokeWidth={2} className={spin ? 'spin' : undefined} />
      </div>
      <div className="hero__body">
        {name !== undefined ? (
          <>
            {kind && <div className="hero__kind">{kind}</div>}
            <h1 className="hero__name">
              <span style={{ minWidth: 0 }}>{name}</span>
              {titleAction}
            </h1>
            {meta && <div className="hero__meta">{meta}</div>}
          </>
        ) : (
          children
        )}
      </div>
      {lastUpdated && (
        <div className="hero__updated">
          <div className="hero__updated-label">Last Updated</div>
          <div className="hero__updated-value">{new Date(lastUpdated).toLocaleTimeString()}</div>
          <div className="muted" style={{ fontSize: 12 }} title={formatDateTime(lastUpdated)}>
            {new Date(lastUpdated).toLocaleDateString()}
          </div>
        </div>
      )}
    </>
  );
  return to ? (
    <Link to={to} className={`hero hero--${color}`}>
      {body}
    </Link>
  ) : (
    <section className={`hero hero--${color}`}>{body}</section>
  );
}
