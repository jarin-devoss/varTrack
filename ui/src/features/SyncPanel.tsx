import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useApi } from '../api';
import type { RuleView } from '../api/types';
import { Notice, Timestamp } from '../components/common';
import { SINK_LABEL } from '../components/Icon';
import { Dialog, SlideOver } from '../components/Overlay';
import { DriftPill, TaskPill } from '../components/Status';
import { useToast } from '../components/Toasts';

const PRODUCTION_LIKE = /^(prod|production|main|master|live)$/i;

export function isProductionLike(env: string): boolean {
  return PRODUCTION_LIKE.test(env);
}

interface Props {
  rule: RuleView | null;
  open: boolean;
  onClose: () => void;
  initialEnvs?: string[];
  initialDryRun?: boolean;
  onStarted?: (taskIds: string[]) => void;
}

export function SyncPanel({ rule, open, onClose, initialEnvs, initialDryRun = false, onStarted }: Props) {
  const api = useApi();
  const notify = useToast();
  const [envs, setEnvs] = useState<string[]>([]);
  const [dryRun, setDryRun] = useState(initialDryRun);
  const [prune, setPrune] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmText, setConfirmText] = useState('');

  useEffect(() => {
    if (!open || !rule) return;
    setEnvs(initialEnvs?.length ? initialEnvs : rule.targets.map(t => t.env));
    setDryRun(initialDryRun || rule.dryRun);
    setPrune(false);
    setError(null);
    setConfirming(false);
    setConfirmText('');
    // Reset only when the panel opens, not on every background refresh.
  }, [open, rule?.name]);

  if (!rule) return null;

  const selected = rule.targets.filter(t => envs.includes(t.env));
  const riskyPrune = prune && !dryRun && selected.some(t => isProductionLike(t.env));
  const file = rule.fileName ?? `${Object.keys(rule.filePathMap).length} path pattern(s)`;
  const toggle = (env: string) => setEnvs(e => (e.includes(env) ? e.filter(x => x !== env) : [...e, env]));

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const { taskIds } = await api.sync({ rule: rule.name, envs, dryRun, prune });
      notify({
        tone: 'progress',
        title: dryRun ? 'Dry run started' : `Sync started for ${envs.length} environment${envs.length === 1 ? '' : 's'}`,
        body:
          taskIds.length === 1 ? (
            <Link to={`/tasks/${taskIds[0]}`}>Follow the task</Link>
          ) : (
            <Link to="/tasks">Follow {taskIds.length} tasks</Link>
          ),
      });
      onStarted?.(taskIds);
      setConfirming(false);
      onClose();
    } catch (err) {
      setError((err as Error).message);
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };

  const onPrimary = () => (riskyPrune ? setConfirming(true) : void submit());
  const primaryLabel = dryRun
    ? `Start dry run${envs.length > 1 ? ` (${envs.length})` : ''}`
    : `Sync ${envs.length} environment${envs.length === 1 ? '' : 's'}`;

  return (
    <>
      <SlideOver
        open={open}
        onClose={onClose}
        title={dryRun ? 'Dry run' : 'Sync from Git'}
        subtitle={<span className="mono">{rule.name}</span>}
        actions={
          <>
            <button className="btn btn--primary" onClick={onPrimary} disabled={busy || envs.length === 0}>
              {busy ? 'Starting…' : primaryLabel}
            </button>
            <button className="btn" onClick={onClose}>
              Cancel
            </button>
          </>
        }
      >
        <div className="stack">
          <p className="secondary">
            Reads <code>{file}</code> at the head of each environment's Git ref, validates it against the CUE schema and{' '}
            {dryRun ? 'reports what would change, without writing anything.' : `writes it to ${SINK_LABEL[rule.sinkType]}.`}
          </p>

          {rule.dryRun && (
            <Notice tone="info">
              This rule has <code>dry_run: true</code> in the bundle, so every sync is a simulation.
            </Notice>
          )}

          <fieldset style={{ border: 0, margin: 0, padding: 0 }}>
            <div className="row" style={{ marginBottom: 4 }}>
              <legend className="field__label">Environments</legend>
              <span className="spacer" />
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => setEnvs(envs.length === rule.targets.length ? [] : rule.targets.map(t => t.env))}
              >
                {envs.length === rule.targets.length ? 'Clear' : 'Select all'}
              </button>
            </div>
            <div className="card">
              {rule.targets.map(t => (
                <label key={t.env} className="check" style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
                  <input type="checkbox" checked={envs.includes(t.env)} onChange={() => toggle(t.env)} />
                  <span className="check__text" style={{ flex: 1 }}>
                    <span className="row">
                      <strong>{t.env}</strong>
                      {isProductionLike(t.env) && <span className="tag">production</span>}
                      <span className="spacer" />
                      <DriftPill state={t.drift} />
                    </span>
                    <span className="check__hint">
                      <span className="mono">{t.ref}</span> → <span className="mono">{t.destination}</span>
                    </span>
                    {t.lastSync && (
                      <span className="check__hint row" style={{ gap: 6 }}>
                        Last sync <TaskPill state={t.lastSync.state} /> <Timestamp iso={t.lastSync.finishedAt ?? t.lastSync.startedAt} />
                      </span>
                    )}
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <div>
            <div className="field__label" style={{ marginBottom: 2 }}>Options</div>
            <label className="check">
              <input type="checkbox" checked={dryRun} disabled={rule.dryRun} onChange={e => setDryRun(e.target.checked)} />
              <span className="check__text">
                <span>Dry run</span>
                <span className="check__hint">Run parse, validation and diff without writing. Secret values are masked.</span>
              </span>
            </label>
            <label className="check" style={{ opacity: rule.prune.enabled ? 1 : 0.6 }}>
              <input type="checkbox" checked={prune} disabled={!rule.prune.enabled} onChange={e => setPrune(e.target.checked)} />
              <span className="check__text">
                <span>Prune keys that are no longer in Git</span>
                <span className="check__hint">
                  {rule.prune.enabled
                    ? rule.prune.protection.length
                      ? <>Protected from pruning: {rule.prune.protection.map(p => <code key={p} style={{ marginRight: 6 }}>{p}</code>)}</>
                      : 'No keys are protected from pruning on this rule.'
                    : 'Pruning is turned off for this rule in the bundle.'}
                </span>
              </span>
            </label>
          </div>

          {riskyPrune && (
            <Notice tone="warning">
              <strong>Prune writes to production.</strong> Keys missing from Git will be deleted from{' '}
              {selected.filter(t => isProductionLike(t.env)).map(t => <code key={t.env}>{t.destination}</code>)}. You'll be asked to confirm.
            </Notice>
          )}
          {error && <Notice tone="critical">{error}</Notice>}
        </div>
      </SlideOver>

      <Dialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title="Prune production keys?"
        footer={
          <>
            <button className="btn" onClick={() => setConfirming(false)}>
              Cancel
            </button>
            <button className="btn btn--danger" disabled={confirmText !== 'prune' || busy} onClick={() => void submit()}>
              {busy ? 'Starting…' : 'Sync and prune'}
            </button>
          </>
        }
      >
        <div className="stack">
          <p className="secondary">
            Keys that exist in the sink but not in Git will be deleted. Protected patterns are kept. This can't be undone from varTrack —
            run a dry run first if you're unsure.
          </p>
          <label className="field">
            <span className="field__label">
              Type <code>prune</code> to confirm
            </span>
            <input className="input" value={confirmText} onChange={e => setConfirmText(e.target.value)} autoComplete="off" data-autofocus />
          </label>
        </div>
      </Dialog>
    </>
  );
}
