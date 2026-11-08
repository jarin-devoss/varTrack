import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useApi, type VarTrackApi } from '../api';
import type { RuleView, Task } from '../api/types';
import { Notice, Timestamp } from '../components/common';
import { SINK_LABEL } from '../components/Icon';
import { Dialog, SlideOver } from '../components/Overlay';
import { DriftPill, TaskPill } from '../components/Status';
import { useToast } from '../components/Toasts';
import { prunes, syncableEnvs } from '../lib/rules';
import { environmentsLabel, keyCount, pruneImpact, someKeys, syncOutcome } from '../lib/outcome';
import { isActive } from '../lib/status';

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
  /** Called when the sync is accepted, and again as soon as its tasks have finished: reload what shows them. */
  onChanged?: () => void;
}

/**
 * Ask about the tasks a sync started until they have all finished (a sync takes well under a second; a minute at most is
 * waited). Returns the tasks as they last were, and whether they all ended. Without this the page would show the sync
 * running until its next poll, seconds later, and nothing would say how it went.
 */
async function untilFinished(api: VarTrackApi, taskIds: string[], limitMs = 60_000): Promise<{ tasks: Task[]; ended: boolean }> {
  const deadline = Date.now() + limitMs;
  const last = new Map<string, Task>();
  let pending = [...taskIds];
  for (let wait = 150; pending.length > 0 && Date.now() < deadline; wait = Math.min(wait * 2, 2000)) {
    await new Promise(resolve => setTimeout(resolve, wait));
    const found = await Promise.all(pending.map(id => api.getTask(id).catch(() => undefined)));
    found.forEach((task, i) => task && last.set(pending[i], task));
    pending = pending.filter(id => {
      const task = last.get(id);
      return !task || isActive(task.state);
    });
  }
  return { tasks: taskIds.flatMap(id => last.get(id) ?? []), ended: pending.length === 0 };
}

/** What the panel knows of what this sync would delete: asked of a dry run, which never blocks the sync. */
type Preview = { state: 'off' } | { state: 'loading' } | { state: 'failed' } | { state: 'ready'; wouldPrune: Record<string, string[]> };

export function SyncPanel({ rule, open, onClose, initialEnvs, initialDryRun = false, onChanged }: Props) {
  const api = useApi();
  const notify = useToast();
  const [envs, setEnvs] = useState<string[]>([]);
  const [dryRun, setDryRun] = useState(initialDryRun);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [preview, setPreview] = useState<Preview>({ state: 'off' });
  // Signed in, and the permission engine says no environment of the rule may be synced by this
  // user (no push access to the branches that feed them): dry runs only.
  const readOnly = rule?.canSync === false;
  // An environment no branch feeds any more (an earlier version of the rule wrote it) cannot be synced.
  const fedEnvs = syncableEnvs(rule?.targets ?? []);

  useEffect(() => {
    if (!open || !rule) return;
    const fedNow = syncableEnvs(rule.targets);
    setEnvs(initialEnvs?.length ? initialEnvs.filter(e => fedNow.includes(e)) : fedNow);
    setDryRun(initialDryRun || readOnly);
    setError(null);
    setConfirming(false);
    setConfirmText('');
    // Reset only when the panel opens, not on every background refresh.
  }, [open, rule?.name]);

  // A sync that prunes: ask a dry run which keys it would delete from the environments chosen, so the panel can say so
  // (and ask for a typed word only when a production-like environment would lose keys). The dry run is a preview: it
  // is not listed among the tasks and says nothing in the events. It never blocks the sync: ten seconds, then "can't tell".
  const chosen = envs.join(',');
  const asksWhatItDeletes = open && !!rule && rule.prune.enabled && !dryRun && envs.length > 0;
  useEffect(() => {
    if (!asksWhatItDeletes || !rule) {
      setPreview({ state: 'off' });
      return;
    }
    let current = true;
    setPreview({ state: 'loading' });
    void (async () => {
      try {
        const { taskIds } = await api.sync({ rule: rule.name, tenant: rule.tenant, envs, dryRun: true, prune: false, preview: true });
        const { tasks, ended } = await untilFinished(api, taskIds, 10_000);
        if (!current) return;
        if (!ended || tasks.some(t => t.state === 'FAILED')) return setPreview({ state: 'failed' });
        const wouldPrune: Record<string, string[]> = {};
        for (const t of tasks) for (const [env, ks] of Object.entries(t.wouldPrune ?? {})) wouldPrune[env] = [...(wouldPrune[env] ?? []), ...ks];
        setPreview({ state: 'ready', wouldPrune });
      } catch {
        if (current) setPreview({ state: 'failed' });
      }
    })();
    return () => {
      current = false;
    };
  }, [asksWhatItDeletes, rule?.name, rule?.tenant, chosen]);

  if (!rule) return null;

  const selected = rule.targets.filter(t => envs.includes(t.env));
  // The rule decides whether a sync prunes (the panel cannot turn it on or off), so the confirmation for a
  // production-like environment applies whenever this sync will delete keys, not only when a box is ticked.
  // When the dry run has said which keys go, only an environment that loses keys counts; when it could not say, any
  // production-like environment does (today's rule). A sync that deletes nothing asks nothing.
  const impact = preview.state === 'ready' ? pruneImpact(preview.wouldPrune, envs) : null;
  const productionLike = (envs: string[]) => envs.some(isProductionLike);
  const riskyPrune =
    prunes(rule.prune, dryRun) && (impact ? productionLike(impact.byEnv.map(([env]) => env)) : productionLike(selected.map(t => t.env)));
  const file = rule.fileName ?? `${Object.keys(rule.filePathMap).length} path pattern(s)`;
  const toggle = (env: string) => setEnvs(e => (e.includes(env) ? e.filter(x => x !== env) : [...e, env]));

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const { taskIds } = await api.sync({ rule: rule.name, tenant: rule.tenant, envs, dryRun, prune: false });
      const what = environmentsLabel(envs);
      const follow =
        taskIds.length === 1 ? <Link to={`/tasks/${taskIds[0]}`}>Follow the task</Link> : <Link to="/tasks">Follow {taskIds.length} tasks</Link>;
      // The toast says the sync is running, then how it ended: it is the same toast, updated.
      const toast = notify({ tone: 'progress', title: dryRun ? `Dry run of ${what}…` : `Syncing ${what}…`, body: follow });
      const syncedEnvs = [...envs];
      const asDryRun = dryRun;
      onChanged?.();
      void untilFinished(api, taskIds).then(({ tasks, ended }) => {
        onChanged?.();
        if (!ended) {
          notify.update(toast, { tone: 'progress', title: `Still running: ${what}`, body: follow });
          return;
        }
        const outcome = syncOutcome(tasks, syncedEnvs, asDryRun);
        notify.update(toast, {
          tone: outcome.tone,
          title: outcome.title,
          sticky: outcome.tone === 'critical',
          body: (
            <>
              {outcome.detail}
              {outcome.taskId && (
                <>
                  {' · '}
                  <Link to={`/tasks/${outcome.taskId}`}>{outcome.tone === 'critical' ? 'Open the task' : 'Details'}</Link>
                </>
              )}
            </>
          ),
        });
      });
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
  // The button says what it does: with keys to delete (and no typed word to ask for) it says so.
  const primaryLabel = dryRun
    ? `Start dry run${envs.length > 1 ? ` (${envs.length})` : ''}`
    : impact && impact.total > 0 && !riskyPrune
      ? `Sync and delete ${keyCount(impact.total)}`
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
            <button
              className="btn btn--primary"
              onClick={onPrimary}
              disabled={busy || envs.length === 0}
              title={envs.length === 0 ? (fedEnvs.length === 0 ? 'No branch feeds any environment of this rule' : 'Choose an environment first') : undefined}
            >
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

          <fieldset style={{ border: 0, margin: 0, padding: 0 }}>
            <div className="row" style={{ marginBottom: 4 }}>
              <legend className="field__label">Environments</legend>
              <span className="spacer" />
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => setEnvs(envs.length === fedEnvs.length ? [] : fedEnvs)}
              >
                {envs.length === fedEnvs.length ? 'Clear' : 'Select all'}
              </button>
            </div>
            <div className="card">
              {rule.targets.map(t => (
                <label key={t.env} className="check" style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
                  <input
                    type="checkbox"
                    checked={envs.includes(t.env)}
                    disabled={!!t.notFed}
                    aria-label={`Environment ${t.env}`}
                    onChange={() => toggle(t.env)}
                  />
                  <span className="check__text" style={{ flex: 1 }}>
                    <span className="row">
                      <strong>{t.env}</strong>
                      {isProductionLike(t.env) && <span className="tag">production</span>}
                      <span className="spacer" />
                      <DriftPill state={t.drift} />
                    </span>
                    <span className="check__hint">
                      <span className="mono">{t.fedBy ?? t.ref}</span> → <span className="mono">{t.destination}</span>
                    </span>
                    {t.notFed && <span className="check__hint">{t.notFed}</span>}
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
              <input type="checkbox" checked={dryRun} disabled={readOnly} onChange={e => setDryRun(e.target.checked)} />
              <span className="check__text">
                <span>Dry run</span>
                <span className="check__hint">
                  {readOnly
                    ? "You can read this rule's repository but not write to it, so only dry runs are allowed."
                    : 'Run parse, validation and diff without writing. Secret values are masked.'}
                </span>
              </span>
            </label>
            <label className="check" style={{ opacity: rule.prune.enabled ? 1 : 0.6 }}>
              {/* Shown, not chosen: whether a sync prunes is the rule's setting. */}
              <input type="checkbox" checked={prunes(rule.prune, dryRun)} disabled readOnly aria-label="Prune keys that are no longer in Git" />
              <span className="check__text">
                <span>Prune keys that are no longer in Git</span>
                <span className="check__hint">
                  {!rule.prune.enabled ? (
                    'Pruning is turned off for this rule in the bundle.'
                  ) : (
                    <>
                      This rule prunes on every sync.{' '}
                      {rule.prune.protection.length ? (
                        <>Protected from pruning: {rule.prune.protection.map(p => <code key={p} style={{ marginRight: 6 }}>{p}</code>)}</>
                      ) : (
                        'No keys are protected from pruning.'
                      )}
                    </>
                  )}
                </span>
                {rule.prune.enabled && !dryRun && (
                  <span className="check__hint" role="status" data-testid="prune-preview">
                    {preview.state === 'loading' && 'Checking what this sync deletes…'}
                    {preview.state === 'failed' && "Couldn't tell what this sync deletes."}
                    {impact && impact.total === 0 && 'This sync deletes nothing.'}
                    {impact && impact.total > 0 && (
                      <>
                        This sync deletes {keyCount(impact.total)}:{' '}
                        {impact.byEnv.map(([env, ks], i) => (
                          <span key={env}>
                            {i > 0 && '; '}
                            {env} — <code>{someKeys(ks)}</code>
                          </span>
                        ))}
                      </>
                    )}
                  </span>
                )}
              </span>
            </label>
          </div>

          {fedEnvs.length === 0 && (
            <Notice tone="info">
              <strong>Nothing to {dryRun ? 'dry-run' : 'sync'}.</strong> No branch feeds any environment of this rule now. A dry run is a sync
              that writes nothing, but it still reads the branch that feeds the environment, and there is none to read. Map a branch to
              an environment (<code>branch_map</code>) or turn <code>branch_as_env</code> on.
            </Notice>
          )}

          {riskyPrune && (
            <Notice tone="warning">
              <strong>Prune writes to production.</strong> Keys missing from Git will be deleted from{' '}
              {(impact ? selected.filter(t => impact.byEnv.some(([env]) => env === t.env)) : selected)
                .filter(t => isProductionLike(t.env))
                .map(t => <code key={t.env}>{t.destination}</code>)}
              . You'll be asked to confirm.
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
          {impact ? (
            <>
              <p className="secondary">These keys exist in the sink and not in Git, and will be deleted. Protected patterns are kept. This can't be undone from varTrack.</p>
              {impact.byEnv
                .filter(([env]) => isProductionLike(env))
                .map(([env, ks]) => (
                  <div key={env}>
                    <strong>
                      Keys to be deleted from {env} ({ks.length}):
                    </strong>
                    <ul style={{ maxHeight: 200, overflowY: 'auto', margin: '6px 0 0', paddingLeft: 20 }}>
                      {ks.map(k => (
                        <li key={k}>
                          <code>{k}</code>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
            </>
          ) : (
            <p className="secondary">
              Keys that exist in the sink but not in Git will be deleted. Protected patterns are kept. This can't be undone from varTrack. The
              panel couldn't tell which keys; run a dry run first if you're unsure.
            </p>
          )}
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
