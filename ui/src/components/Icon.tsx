import type { SVGProps } from 'react';
import type { SinkType } from '../api/types';

// 24×24 stroke icons drawn for this UI. Color comes from currentColor.
const PATHS = {
  overview: (
    <>
      <rect x="3" y="3" width="7.5" height="9" rx="1.5" />
      <rect x="13.5" y="3" width="7.5" height="5" rx="1.5" />
      <rect x="13.5" y="11" width="7.5" height="10" rx="1.5" />
      <rect x="3" y="15" width="7.5" height="6" rx="1.5" />
    </>
  ),
  rules: (
    <>
      <circle cx="6" cy="5.5" r="2.5" />
      <circle cx="6" cy="18.5" r="2.5" />
      <circle cx="18" cy="12" r="2.5" />
      <path d="M6 8v8M8.2 6.8C12 8 12.5 12 15.5 12" />
    </>
  ),
  activity: <path d="M3 12h4l3-8 4 16 3-8h4" />,
  tasks: (
    <>
      <path d="M10 6h11M10 12h11M10 18h11" />
      <path d="m3 6 1.5 1.5L7 5M3 12l1.5 1.5L7 11M3 18l1.5 1.5L7 17" />
    </>
  ),
  playground: (
    <>
      <path d="M9 3h6M10 3v6.2L4.6 18.4A1.7 1.7 0 0 0 6.1 21h11.8a1.7 1.7 0 0 0 1.5-2.6L14 9.2V3" />
      <path d="M7.5 15h9" />
    </>
  ),
  settings: (
    <>
      <path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" />
      <circle cx="15" cy="6" r="2" />
      <circle cx="9" cy="12" r="2" />
      <circle cx="17" cy="18" r="2" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </>
  ),
  x: <path d="M6 6l12 12M18 6 6 18" />,
  filter: <path d="M3 4h18l-7 8.5V19l-4 2v-8.5z" />,
  grid: (
    <>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.2" />
      <rect x="13.5" y="3.5" width="7" height="7" rx="1.2" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.2" />
      <rect x="13.5" y="13.5" width="7" height="7" rx="1.2" />
    </>
  ),
  list: <path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" />,
  barChart: <path d="M4 20h16M7 16v-5M12 16V6M17 16v-8" />,
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  checkCircle: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m8 12.3 2.7 2.7L16 9.7" />
    </>
  ),
  alertTriangle: (
    <>
      <path d="M10.3 3.9 2.4 17.6A2 2 0 0 0 4.1 20.6h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
      <path d="M12 9.5v4.2M12 17h.01" />
    </>
  ),
  xCircle: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m9 9 6 6M15 9l-6 6" />
    </>
  ),
  alertCircle: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7.5v5.5M12 16.5h.01" />
    </>
  ),
  helpCircle: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.3-2.4 3.6M12 17h.01" />
    </>
  ),
  loader: <path d="M21 12a9 9 0 1 1-6.2-8.6" />,
  refresh: (
    <>
      <path d="M20 11.5A8 8 0 0 0 6.3 6.3L4 8.5M4 12.5a8 8 0 0 0 13.7 5.2L20 15.5" />
      <path d="M4 4v4.5h4.5M20 20v-4.5h-4.5" />
    </>
  ),
  eyeOff: (
    <>
      <path d="M10.6 5.1A9.8 9.8 0 0 1 12 5c5 0 8.5 4.5 9.5 7a13 13 0 0 1-2.4 3.5M6.6 6.6C4.4 8 3 10.2 2.5 12c1 2.5 4.5 7 9.5 7a9.7 9.7 0 0 0 5.4-1.6" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2M3 3l18 18" />
    </>
  ),
  arrowUpCircle: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16.5v-9M8.5 11 12 7.5l3.5 3.5" />
    </>
  ),
  pauseCircle: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M10 9v6M14 9v6" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  minusCircle: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M8 12h8" />
    </>
  ),
  gitBranch: (
    <>
      <circle cx="6" cy="5.5" r="2.5" />
      <circle cx="6" cy="18.5" r="2.5" />
      <circle cx="18" cy="7" r="2.5" />
      <path d="M6 8v8M18 9.5c0 4-5 4-11 7.5" />
    </>
  ),
  gitCommit: (
    <>
      <circle cx="12" cy="12" r="3.5" />
      <path d="M3 12h5.5M15.5 12H21" />
    </>
  ),
  file: (
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </>
  ),
  database: (
    <>
      <ellipse cx="12" cy="5.5" rx="7.5" ry="2.8" />
      <path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8" />
    </>
  ),
  layers: (
    <>
      <path d="m12 3 9 4.5-9 4.5-9-4.5z" />
      <path d="m3 12 9 4.5 9-4.5M3 16.5 12 21l9-4.5" />
    </>
  ),
  nodes: (
    <>
      <circle cx="12" cy="5" r="2.3" />
      <circle cx="5.5" cy="18" r="2.3" />
      <circle cx="18.5" cy="18" r="2.3" />
      <path d="M11 7 6.6 16M13 7l4.4 9M7.8 18h8.4" />
    </>
  ),
  bucket: (
    <>
      <path d="M4 6h16l-1.8 13a2 2 0 0 1-2 1.7H7.8a2 2 0 0 1-2-1.7z" />
      <ellipse cx="12" cy="6" rx="8" ry="2.3" />
    </>
  ),
  fileCog: (
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
      <circle cx="12" cy="14.5" r="2.2" />
      <path d="M12 10.8v1.5M12 16.7v1.5M8.3 14.5h1.5M14.2 14.5h1.5" />
    </>
  ),
  anchor: (
    <>
      <circle cx="12" cy="5" r="2.3" />
      <path d="M12 7.3V21M8 11h8M4.5 13.5A7.5 7.5 0 0 0 12 21a7.5 7.5 0 0 0 7.5-7.5" />
    </>
  ),
  triangle: <path d="M12 4 21 20H3z" />,
  terminal: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="m7 9 3 3-3 3M12.5 15H17" />
    </>
  ),
  server: (
    <>
      <rect x="3" y="3.5" width="18" height="7" rx="1.8" />
      <rect x="3" y="13.5" width="18" height="7" rx="1.8" />
      <path d="M7 7h.01M7 17h.01" />
    </>
  ),
  chevronLeft: <path d="m15 5-7 7 7 7" />,
  chevronRight: <path d="m9 5 7 7-7 7" />,
  chevronDown: <path d="m5 9 7 7 7-7" />,
  chevronUp: <path d="m5 15 7-7 7 7" />,
  arrowRight: <path d="M4 12h15M13 6l6 6-6 6" />,
  arrowLeft: <path d="M20 12H5M11 6l-6 6 6 6" />,
  moreVertical: <path d="M12 5h.01M12 12h.01M12 19h.01" strokeWidth={3} />,
  plus: <path d="M12 5v14M5 12h14" />,
  sitemap: (
    <>
      <rect x="9" y="3" width="6" height="5" rx="1" />
      <rect x="3" y="16" width="6" height="5" rx="1" />
      <rect x="15" y="16" width="6" height="5" rx="1" />
      <path d="M12 8v4M6 16v-4h12v4" />
    </>
  ),
  pieChart: (
    <>
      <path d="M21 12A9 9 0 1 1 12 3v9z" />
      <path d="M15 3.5A9 9 0 0 1 20.5 9H15z" />
    </>
  ),
  ruler: (
    <>
      <rect x="2.5" y="8" width="19" height="8" rx="1.5" />
      <path d="M6.5 8v3M10.5 8v4M14.5 8v3M18.5 8v4" />
    </>
  ),
  star: <path d="m12 3.5 2.6 5.3 5.9.8-4.3 4.2 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.2 5.9-.8z" />,
  external: (
    <>
      <path d="M14 4h6v6M20 4l-9 9" />
      <path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4" />
    </>
  ),
  copy: (
    <>
      <rect x="8.5" y="8.5" width="12" height="12" rx="2" />
      <path d="M15.5 8.5V5.5a2 2 0 0 0-2-2h-8a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h3" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="15" r="4.5" />
      <path d="m11.3 11.7 8.2-8.2M17 6l2.5 2.5M14.5 8.5l2 2" />
    </>
  ),
  shield: <path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.2 7.5 9.5 4.3-1.3 7.5-4.9 7.5-9.5V6z" />,
  lock: (
    <>
      <rect x="4.5" y="10.5" width="15" height="10" rx="2" />
      <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" />
    </>
  ),
  play: <path d="M7 4.5v15l12-7.5z" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4" />
    </>
  ),
  moon: <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />,
  monitor: (
    <>
      <rect x="3" y="4" width="18" height="12.5" rx="2" />
      <path d="M8.5 20.5h7M12 16.5v4" />
    </>
  ),
  keyboard: (
    <>
      <rect x="2.5" y="5.5" width="19" height="13" rx="2" />
      <path d="M6.5 9.5h.01M10 9.5h.01M13.5 9.5h.01M17 9.5h.01M6.5 13h.01M17 13h.01M9.5 15h5" />
    </>
  ),
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  panelLeft: (
    <>
      <rect x="3" y="3.5" width="18" height="17" rx="2" />
      <path d="M9 3.5v17" />
    </>
  ),
  plug: (
    <>
      <path d="M9 3v4.5M15 3v4.5M6.5 7.5h11V11a5.5 5.5 0 0 1-11 0z" />
      <path d="M12 16.5V21" />
    </>
  ),
  pulse: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M6.3 6.3a8 8 0 0 0 0 11.4M17.7 6.3a8 8 0 0 1 0 11.4" />
    </>
  ),
  diff: (
    <>
      <path d="M12 3v8M8 7h8M8 17h8" />
      <rect x="4" y="3" width="16" height="18" rx="2" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="4" />
      <path d="M4.5 20.5a7.5 7.5 0 0 1 15 0" />
    </>
  ),
  webhook: (
    <>
      <path d="M9 9.5a4 4 0 1 1 6.8 2.8L12.5 18" />
      <path d="M6.5 18a4 4 0 1 1 1.1-7.8" />
      <path d="M17.5 18a4 4 0 1 0-1.1-7.8" />
      <path d="M8 18h8" />
    </>
  ),
  trash: (
    <>
      <path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13" />
    </>
  ),
} as const;

export type IconName = keyof typeof PATHS;

interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName;
  size?: number;
  label?: string;
}

export function Icon({ name, size = 16, label, className, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}

export const SINK_ICON: Record<SinkType, IconName> = {
  mongo: 'database',
  redis: 'layers',
  zookeeper: 'nodes',
  s3: 'bucket',
  configmap: 'fileCog',
  helm: 'anchor',
  vercel: 'triangle',
  linux_server: 'terminal',
};

export const SINK_LABEL: Record<SinkType, string> = {
  mongo: 'MongoDB',
  redis: 'Redis',
  zookeeper: 'ZooKeeper',
  s3: 'S3',
  configmap: 'ConfigMap',
  helm: 'Helm',
  vercel: 'Vercel',
  linux_server: 'Linux server',
};
