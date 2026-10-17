import { useEffect, useState } from 'react';
import { useLocation, useParams } from 'react-router-dom';
import { createLiveApi } from '../api/live';
import type { ServiceHealth } from '../api/types';
import { ErrorState, LoadingRows, Notice, Segmented } from '../components/common';
import { Icon, SINK_ICON, SINK_LABEL } from '../components/Icon';
import { Panel } from '../components/Panel';
import { useToast } from '../components/Toasts';
import { usePageTitle } from '../layout/page';
import { DEFAULT_PREFS, usePrefs, type ConnectionPrefs, type Theme } from '../lib/prefs';
import { useQuery } from '../lib/useQuery';
import { ServiceRows } from './DashboardPage';

type Availability = 'available' | 'stub' | 'proposed';

const ENDPOINTS: { screen: string; method: string; path: string; service: string; status: Availability }[] = [
  { screen: 'Service health', method: 'GET', path: '/health/readiness', service: 'gateway, watcher', status: 'available' },
  { screen: 'Service health', method: 'GET', path: '/v1/health/readiness', service: 'orchestrator', status: 'available' },
  { screen: 'Rules, environments, dashboard', method: 'GET', path: '/v1/ui/rules[/{platform}/{datasource}]', service: 'orchestrator', status: 'available' },
  { screen: 'Keys and diff', method: 'GET', path: '/v1/ui/rules/{platform}/{datasource}/targets/{env}/keys', service: 'orchestrator', status: 'available' },
  { screen: 'Sync a rule from Git', method: 'POST', path: '/v1/ui/rules/{platform}/{datasource}/sync', service: 'orchestrator', status: 'available' },
  { screen: 'Events', method: 'GET', path: '/v1/ui/events', service: 'orchestrator', status: 'available' },
  { screen: 'Tasks, task page', method: 'GET', path: '/v1/ui/tasks[/{task_id}]', service: 'orchestrator', status: 'available' },
  { screen: 'Tenant, datasources', method: 'GET', path: '/v1/ui/bundle', service: 'orchestrator', status: 'available' },
  { screen: 'Playground: validate', method: 'POST', path: '/v1/cli/validate', service: 'gateway (needs auth in the bundle)', status: 'available' },
  { screen: 'Playground: dry run, sync', method: 'POST', path: '/v1/cli/sync', service: 'gateway (needs auth in the bundle)', status: 'available' },
];

const AVAILABILITY: Record<Availability, { label: string; color: string }> = {
  available: { label: 'Available', color: 'green' },
  stub: { label: 'Returns 501', color: 'yellow' },
  proposed: { label: 'Not implemented', color: 'gray' },
};

export function SettingsPage() {
  usePageTitle('Settings');
  const { section } = useParams();
  const { hash } = useLocation();
  useEffect(() => {
    const id = section ?? hash.slice(1);
    if (id) document.getElementById(id)?.scrollIntoView({ block: 'start' });
  }, [section, hash]);

  return (
    <div className="page-stack">
      <div>
        <h1 style={{ fontSize: 24, fontWeight: 600 }}>Settings</h1>
        <p className="muted" style={{ marginTop: 4 }}>
          Where the UI gets its data, the services behind it, and how it looks. Preferences stay in this browser.
        </p>
      </div>
      <ConnectionSection />
      <ServicesSection />
      <BundleSection />
      <AppearanceSection />
    </div>
  );
}

function ConnectionSection() {
  const [prefs, setPrefs] = usePrefs();
  const notify = useToast();
  const [draft, setDraft] = useState<ConnectionPrefs>(prefs.connection);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<ServiceHealth[] | null>(null);

  useEffect(() => setDraft(prefs.connection), [prefs.connection]);
  const field = (key: keyof ConnectionPrefs) => ({
    value: draft[key],
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setDraft(d => ({ ...d, [key]: e.target.value })),
  });
  const dirty = JSON.stringify(draft) !== JSON.stringify(prefs.connection);

  const test = async () => {
    setTesting(true);
    setResult(await createLiveApi(draft).getHealth());
    setTesting(false);
  };
  const save = () => {
    setPrefs({ connection: draft });
    notify({ tone: 'good', title: 'Connection saved' });
  };

  return (
    <Panel title="Connection" subtitle="Where your varTrack services are" id="connection">
      <div className="stack">
        <div className="fields">
          <label className="field">
            <span className="field__label">Gateway URL</span>
            <input className="input mono" {...field('gatewayUrl')} />
            <span className="field__hint">Webhooks and CLI API</span>
          </label>
          <label className="field">
            <span className="field__label">Orchestrator URL</span>
            <input className="input mono" {...field('orchestratorUrl')} />
            <span className="field__hint">Rules, drift, keys, tasks and events</span>
          </label>
          <label className="field">
            <span className="field__label">Watcher admin URL</span>
            <input className="input mono" {...field('watcherUrl')} />
            <span className="field__hint">Readiness and active watchers</span>
          </label>
          <label className="field">
            <span className="field__label">Tenant</span>
            <input className="input mono" {...field('tenant')} />
            <span className="field__hint">Sent as X-Tenant-ID</span>
          </label>
        </div>
        <p className="field__hint">
          The defaults are the dev server's proxy paths (<code>/api/gateway</code> and so on). The bearer token is set on the sign-in page and
          kept for this tab only.
        </p>
        {result && <ServiceRows services={result} />}
        <div className="row row--wrap">
          <button className="btn" onClick={() => void test()} disabled={testing}>
            <Icon name="pulse" size={14} /> {testing ? 'Testing…' : 'Test connection'}
          </button>
          <span className="spacer" />
          {dirty && (
            <button className="btn btn--ghost" onClick={() => setDraft(prefs.connection)}>
              Discard
            </button>
          )}
          <button className="btn btn--primary" onClick={save} disabled={!dirty}>
            Save
          </button>
        </div>
        <details>
          <summary className="muted" style={{ cursor: 'pointer' }}>
            Which endpoints each screen uses
          </summary>
          <div className="table-wrap" style={{ marginTop: 10 }}>
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Screen</th>
                  <th>Endpoint</th>
                  <th>Service</th>
                  <th>On the server today</th>
                </tr>
              </thead>
              <tbody>
                {ENDPOINTS.map(e => (
                  <tr key={`${e.method} ${e.path}`}>
                    <td>{e.screen}</td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {e.method} {e.path}
                    </td>
                    <td className="muted">{e.service}</td>
                    <td>
                      <span className={`pill pill--${AVAILABILITY[e.status].color}`}>{AVAILABILITY[e.status].label}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </div>
    </Panel>
  );
}

function ServicesSection() {
  const health = useQuery('health', api => api.getHealth(), { poll: 15000 });
  return (
    <Panel title="Services" subtitle="gateway, orchestrator and watcher readiness" id="services">
      {health.data ? <ServiceRows services={health.data} /> : <LoadingRows rows={3} height={40} />}
    </Panel>
  );
}

function BundleSection() {
  const bundle = useQuery('bundle', api => api.getBundle(), { poll: false });
  if (!bundle.data) {
    return (
      <Panel title="Datasources" id="datasources">
        {bundle.error ? <ErrorState compact error={bundle.error} what="the bundle" onRetry={bundle.reload} /> : <LoadingRows rows={4} height={30} />}
      </Panel>
    );
  }
  const b = bundle.data;
  return (
    <>
      <Panel title="Datasources" subtitle={<>From <code>config.cue</code> · tenant <span className="mono">{b.tenant}</span> · read-only</>} id="datasources">
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Datasource</th>
                <th>Type</th>
                <th>Endpoint</th>
                <th>Settings</th>
                <th>Used by</th>
              </tr>
            </thead>
            <tbody>
              {b.datasources.map(d => (
                <tr key={d.name}>
                  <td className="mono" style={{ whiteSpace: 'nowrap' }}>{d.name}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <span className="row" style={{ gap: 6 }}>
                      <Icon name={SINK_ICON[d.type]} size={14} className="muted" /> {SINK_LABEL[d.type]}
                    </span>
                  </td>
                  <td className="mono muted" style={{ fontSize: 12 }}>{d.endpoint}</td>
                  <td>
                    <div className="row row--wrap" style={{ gap: 4 }}>
                      {Object.entries(d.detail).map(([k, v]) => (
                        <span key={k} className="tag">
                          {k}: {v}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="mono" style={{ fontSize: 12.5, whiteSpace: 'nowrap' }}>{d.rules.join(', ') || <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title="Platforms, secrets and schemas" id="platforms">
        <dl className="facts">
          {b.platforms.map(p => (
            <div key={p.name}>
              <dt>Platform</dt>
              <dd>
                <span className="mono">{p.name}</span> · {p.endpoint}
                <div className="muted" style={{ fontSize: 12 }}>events: {p.pushEvent}, {p.prEvent}</div>
              </dd>
            </div>
          ))}
          {b.secretManagers.map(s => (
            <div key={s.name}>
              <dt>Secret manager</dt>
              <dd>
                <span className="mono">{s.name}</span> · {s.type}
                <div className="muted" style={{ fontSize: 12 }}>{s.endpoint}</div>
              </dd>
            </div>
          ))}
          {b.schemaRegistry && (
            <div>
              <dt>Schema registry</dt>
              <dd>
                <span className="mono">
                  {b.schemaRegistry.repo}@{b.schemaRegistry.branch}
                </span>
                <div className="muted" style={{ fontSize: 12 }}>{b.schemaRegistry.files.length} CUE files</div>
              </dd>
            </div>
          )}
        </dl>
        <div style={{ marginTop: 16 }}>
          <Notice tone="info">
            The bundle comes from <code>config.cue</code>. Change it in Git.
          </Notice>
        </div>
      </Panel>
    </>
  );
}

function AppearanceSection() {
  const [prefs, setPrefs] = usePrefs();
  const notify = useToast();
  return (
    <Panel title="Appearance" id="appearance">
      <div className="stack">
        <div className="row row--wrap" style={{ gap: 16 }}>
          <span className="field__label" style={{ width: 120 }}>Theme</span>
          <Segmented<Theme>
            label="Theme"
            value={prefs.theme}
            onChange={theme => setPrefs({ theme })}
            options={[
              { value: 'system', label: <><Icon name="monitor" size={14} /> System</> },
              { value: 'light', label: <><Icon name="sun" size={14} /> Light</> },
              { value: 'dark', label: <><Icon name="moon" size={14} /> Dark</> },
            ]}
          />
        </div>
        <label className="check">
          <input type="checkbox" checked={prefs.live} onChange={e => setPrefs({ live: e.target.checked })} />
          <span className="check__text">
            <span>Live updates</span>
            <span className="check__hint">Refresh open pages every few seconds while the tab is visible. Shortcut: L.</span>
          </span>
        </label>
        <div>
          <button
            className="btn btn--sm"
            onClick={() => {
              setPrefs({ ...DEFAULT_PREFS, connection: prefs.connection });
              notify({ tone: 'good', title: 'Favorites, theme and list settings reset' });
            }}
          >
            Reset favorites and preferences
          </button>
        </div>
      </div>
    </Panel>
  );
}
