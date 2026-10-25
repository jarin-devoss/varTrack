import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useApi } from '../api';
import type { ValidateResult } from '../api/types';
import { Card, CopyButton, EmptyState, Notice } from '../components/common';
import { Icon } from '../components/Icon';
import { Dialog } from '../components/Overlay';
import { StatusIcon, TaskPill } from '../components/Status';
import { usePageTitle } from '../layout/page';
import { useToast } from '../components/Toasts';
import { isProductionLike } from '../features/SyncPanel';
import { shortId } from '../features/tasks';
import { detectFormat, flattenConfig, plaintextSecrets } from '../lib/parse';
import { ruleKey } from '../lib/rules';
import { isActive } from '../lib/status';
import { useQuery } from '../lib/useQuery';

const SAMPLE = `# configs/app.yaml — values are flattened to dotted keys
app:
  name: checkout
  region: us-east-1

database:
  host: mongo.stage.internal
  pool_size: "10"
  password: "@secret(vault:checkout/db#password)"

feature_flags:
  dark_mode: "true"
  new_checkout: "true"

observability:
  log_level: DEBUG
`;

const VALIDATE_LOOK = {
  ok: { icon: 'checkCircle', tone: 'good', label: 'Valid' },
  warn: { icon: 'alertTriangle', tone: 'warning', label: 'Valid, with warnings' },
  failed: { icon: 'xCircle', tone: 'critical', label: 'Invalid' },
} as const;

export function PlaygroundPage() {
  usePageTitle('Playground');
  const api = useApi();
  const notify = useToast();
  const bundle = useQuery('bundle', a => a.getBundle(), { poll: false });

  const [filePath, setFilePath] = useState('configs/app.yaml');
  const [datasource, setDatasource] = useState('');
  const [ruleChoice, setRuleChoice] = useState('');
  const [env, setEnv] = useState('staging');
  const [content, setContent] = useState(SAMPLE);
  const [busy, setBusy] = useState<'validate' | 'dry' | 'sync' | null>(null);
  const [validation, setValidation] = useState<ValidateResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  const datasources = bundle.data?.datasources ?? [];
  const ds = datasource || datasources[0]?.name || '';
  // Several rules, of one or more tenants, may write to one datasource: the request then names the rule's
  // tenant, and its platform when platforms differ.
  const dsRules = useMemo(() => datasources.find(d => d.name === ds)?.rules ?? [], [datasources, ds]);
  const chosen = dsRules.find(r => ruleKey(r) === ruleChoice) ?? dsRules[0];
  const chosenTenant = chosen?.tenant;
  const chosenPlatform = chosen && new Set(dsRules.map(r => r.name.split('/')[0])).size > 1 ? chosen.name.split('/')[0] : undefined;
  const format = detectFormat(filePath);
  const preview = useMemo(() => {
    try {
      const flat = flattenConfig(content, format);
      return { flat, error: null as string | null, secrets: plaintextSecrets(flat) };
    } catch (err) {
      return { flat: {} as Record<string, string>, error: (err as Error).message, secrets: [] as string[] };
    }
  }, [content, format]);
  const keys = Object.entries(preview.flat);

  const run = async (kind: 'validate' | 'dry' | 'sync') => {
    setBusy(kind);
    setError(null);
    try {
      if (kind === 'validate') {
        setValidation(await api.validate({ filePath, content, datasource: ds || undefined, platform: chosenPlatform, tenant: chosenTenant }));
      } else {
        const { taskId: id } = await api.cliSync({ datasource: ds, platform: chosenPlatform, tenant: chosenTenant, env, filePath, content, dryRun: kind === 'dry', label: 'ui-playground' });
        setTaskId(id);
        notify({ tone: 'progress', title: kind === 'dry' ? 'Dry run started' : `Sync to ${env} started`, body: <Link to={`/tasks/${id}`}>Follow the task</Link> });
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
      setConfirm(false);
    }
  };

  const canRun = !!ds && !!env.trim() && !!content.trim() && !!filePath.trim();
  const cli = `vt sync --file ${filePath} --datasource ${ds || '<datasource>'}${chosenPlatform ? ` --platform ${chosenPlatform}` : ''}${chosenTenant && chosenTenant !== 'default' ? ` --tenant ${chosenTenant}` : ''} --env ${env || '<env>'} --dry-run --wait`;

  return (
    <div className="page-stack">
      <div>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 600 }}>Playground</h1>
          <p className="muted" style={{ marginTop: 4 }}>
            Check a config file before you push it: see the keys varTrack will extract, validate them against the CUE schema, or dry-run the
            whole pipeline.
          </p>
        </div>
      </div>

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Card title="Config file">
          <div className="stack">
            <div className="fields">
              <label className="field">
                <span className="field__label">File path</span>
                <input className="input mono" value={filePath} onChange={e => setFilePath(e.target.value)} />
                <span className="field__hint">
                  Format from extension: <span className="tag">{format.toUpperCase()}</span>
                </span>
              </label>
              <label className="field">
                <span className="field__label">Datasource</span>
                {datasources.length ? (
                  <select className="select" value={ds} onChange={e => setDatasource(e.target.value)}>
                    {datasources.map(d => (
                      <option key={d.name} value={d.name}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input className="input mono" placeholder="e.g. mongo" value={datasource} onChange={e => setDatasource(e.target.value)} />
                )}
                <span className="field__hint">Picks the CUE schema and the sink.</span>
              </label>
              {dsRules.length > 1 && chosen && (
                <label className="field">
                  <span className="field__label">Rule</span>
                  <select className="select" value={ruleKey(chosen)} onChange={e => setRuleChoice(e.target.value)}>
                    {dsRules.map(r => (
                      <option key={ruleKey(r)} value={ruleKey(r)}>
                        {r.tenant} · {r.name}
                      </option>
                    ))}
                  </select>
                  <span className="field__hint">{dsRules.length} rules write to {ds}: pick the one to use.</span>
                </label>
              )}
              <label className="field">
                <span className="field__label">Environment</span>
                <input className="input mono" value={env} onChange={e => setEnv(e.target.value)} />
                <span className="field__hint">Used for dry runs and syncs only.</span>
              </label>
            </div>
            <label className="field">
              <span className="field__label">Content</span>
              <textarea className="textarea" value={content} onChange={e => setContent(e.target.value)} spellCheck={false} rows={18} />
            </label>
            <div className="row row--wrap">
              <button className="btn btn--primary" disabled={!content.trim() || busy !== null} onClick={() => void run('validate')}>
                <Icon name="checkCircle" size={15} /> {busy === 'validate' ? 'Validating…' : 'Validate'}
              </button>
              <button className="btn" disabled={!canRun || busy !== null} onClick={() => void run('dry')}>
                <Icon name="play" size={14} /> {busy === 'dry' ? 'Starting…' : 'Dry run'}
              </button>
              <span className="spacer" />
              <button className="btn btn--ghost" disabled={!canRun || busy !== null} onClick={() => setConfirm(true)}>
                Sync to {env || '…'}
              </button>
            </div>
            {error && <Notice tone="critical">{error}</Notice>}
          </div>
        </Card>

        <div className="stack">
          {validation && (
            <Card title="Schema validation">
              <div className="stack stack--tight">
                <span className="badge" style={{ fontWeight: 600 }}>
                  <StatusIcon icon={VALIDATE_LOOK[validation.status].icon} tone={VALIDATE_LOOK[validation.status].tone} />
                  {VALIDATE_LOOK[validation.status].label}
                  <span className="muted" style={{ fontWeight: 400 }}>
                    · {validation.keyCount} keys
                  </span>
                </span>
                {validation.messages.length > 0 && (
                  <ul style={{ margin: '6px 0 0', paddingLeft: 20 }}>
                    {validation.messages.map(m => (
                      <li key={m} className="secondary" style={{ marginBottom: 4 }}>
                        {m}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </Card>
          )}

          {taskId && <TaskProgress key={taskId} taskId={taskId} />}

          <Card title={`Keys varTrack will see${keys.length ? ` (${keys.length})` : ''}`} flush>
            {preview.error ? (
              <EmptyState compact icon="alertCircle" title="Can't preview this file">
                {preview.error}
              </EmptyState>
            ) : keys.length === 0 ? (
              <EmptyState compact icon="file" title="No keys yet">
                Paste a config file to see how it flattens.
              </EmptyState>
            ) : (
              <>
                {preview.secrets.length > 0 && (
                  <div style={{ padding: '12px 16px 0' }}>
                    <Notice tone="warning">
                      {preview.secrets.map(s => <code key={s} style={{ marginRight: 6 }}>{s}</code>)} look like credentials in plain text. Use{' '}
                      <code>@secret(...)</code> so the value stays in your secret manager.
                    </Notice>
                  </div>
                )}
                <div className="table-wrap" style={{ maxHeight: 420 }}>
                  <table className="table table--compact">
                    <thead>
                      <tr>
                        <th>Key</th>
                        <th>Value</th>
                      </tr>
                    </thead>
                    <tbody>
                      {keys.map(([k, v]) => (
                        <tr key={k}>
                          <td className="mono">{k}</td>
                          <td className="value">
                            {v.startsWith('@secret(') ? (
                              <span className="row" style={{ gap: 6 }}>
                                <Icon name="lock" size={13} className="muted" /> <span className="muted">resolved at sync time</span>
                              </span>
                            ) : (
                              v
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </Card>

          <Card title="Same check from the CLI" actions={<CopyButton text={cli} label="Copy command" />}>
            <pre className="code" style={{ whiteSpace: 'pre-wrap' }}>{cli}</pre>
          </Card>
        </div>
      </div>

      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title={`Write this file to ${env}?`}
        footer={
          <>
            <button className="btn" onClick={() => setConfirm(false)}>
              Cancel
            </button>
            <button className={`btn ${isProductionLike(env) ? 'btn--danger' : 'btn--primary'}`} onClick={() => void run('sync')} disabled={busy !== null}>
              {busy === 'sync' ? 'Starting…' : `Sync to ${env}`}
            </button>
          </>
        }
      >
        <div className="stack">
          <p className="secondary">
            This pushes the pasted content straight to <code>{ds}</code> for <code>{env}</code>, bypassing Git — the same as{' '}
            <code>vt sync</code>. The next push to the rule's branch overwrites it, and self-heal may restore the Git version.
          </p>
          {isProductionLike(env) && (
            <Notice tone="warning">
              <strong>{env} looks like a production environment.</strong> Prefer a dry run first.
            </Notice>
          )}
        </div>
      </Dialog>
    </div>
  );
}

function TaskProgress({ taskId }: { taskId: string }) {
  const [done, setDone] = useState(false);
  const task = useQuery(`task:${taskId}`, a => a.getTask(taskId), { poll: done ? false : 1200 });
  useEffect(() => {
    if (task.data) setDone(!isActive(task.data.state));
  }, [task.data?.state]);
  const t = task.data;
  return (
    <Card title="Pipeline run" actions={<Link className="btn btn--ghost btn--sm" to={`/tasks/${taskId}`}>Details</Link>}>
      {t ? (
        <div className="stack stack--tight">
          <span className="row" style={{ gap: 8 }}>
            <TaskPill state={t.state} />
            <span className="mono muted">{shortId(taskId)}</span>
            {t.dryRun && <span className="tag">dry run</span>}
          </span>
          {t.files.map(f => (
            <span key={f.file} className="secondary">
              {f.error ? <span style={{ color: 'var(--status-critical-ink)' }}>{f.error}</span> : t.dryRun ? `${f.keys ?? 0} keys parsed and validated` : `${f.written} keys written`}
            </span>
          ))}
        </div>
      ) : task.error ? (
        <span className="secondary">Started task {shortId(taskId)}. Its status isn't available here: {(task.error as Error).message}</span>
      ) : (
        <span className="secondary">Starting…</span>
      )}
    </Card>
  );
}
