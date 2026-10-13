import { useEffect, useState } from 'react';
import type { BundleView, Rule, SyncMode } from '../api/types';
import { CopyButton, Notice } from '../components/common';
import { SlideOver } from '../components/Overlay';
import { toCue } from './rule/DefinitionTab';

interface Draft {
  platform: string;
  datasource: string;
  repositories: string;
  excludeRepositories: string;
  fileName: string;
  branchMap: string;
  prAsEnv: boolean;
  branchAsEnv: boolean;
  tagAsEnv: boolean;
  destinationTemplate: string;
  syncMode: SyncMode;
  selfHeal: boolean;
  prune: boolean;
  protection: string;
  secretManager: string;
}

const EMPTY: Draft = {
  platform: 'github',
  datasource: '',
  repositories: '',
  excludeRepositories: '',
  fileName: 'configs/app.yaml',
  branchMap: 'main=production\ndevelop=staging',
  prAsEnv: false,
  branchAsEnv: false,
  tagAsEnv: false,
  destinationTemplate: '{env}-config',
  syncMode: 'AUTO',
  selfHeal: true,
  prune: false,
  protection: '',
  secretManager: '',
};

const list = (s: string) => s.split(/[\n,]/).map(x => x.trim()).filter(Boolean);

export function draftToRule(d: Draft, bundle?: BundleView): Rule {
  const branchMap: Record<string, string> = {};
  for (const line of d.branchMap.split('\n')) {
    const [branch, env] = line.split('=').map(x => x?.trim());
    if (branch && env) branchMap[branch] = env;
  }
  return {
    name: `${d.platform}/${d.datasource}`,
    tenant: bundle?.tenant ?? 'default',
    platform: d.platform,
    datasource: d.datasource,
    sinkType: bundle?.datasources.find(x => x.name === d.datasource)?.type ?? 'mongo',
    repositories: list(d.repositories),
    excludeRepositories: list(d.excludeRepositories),
    fileName: d.fileName.trim() || undefined,
    filePathMap: {},
    branchMap,
    branchAsEnv: d.branchAsEnv,
    prAsEnv: d.prAsEnv,
    tagAsEnv: d.tagAsEnv,
    destinationTemplate: d.destinationTemplate.trim(),
    syncMode: d.syncMode,
    selfHeal: d.selfHeal,
    dryRun: false,
    prune: { enabled: d.prune, last: false, dryRun: false, protection: list(d.protection) },
    secretManager: d.secretManager || undefined,
  };
}

/** The checks the bundle schema applies to a rule. */
export function ruleProblems(r: Rule): string[] {
  const problems: string[] = [];
  if (!r.platform) problems.push('platform is required');
  if (!r.datasource) problems.push('datasource is required');
  if (r.repositories.length === 0) problems.push('repositories needs at least one entry');
  if (!r.fileName && Object.keys(r.filePathMap).length === 0) problems.push("provide either 'file_name' or 'file_path_map'");
  const hasEnvStrategy = r.branchAsEnv || r.prAsEnv || r.tagAsEnv || Object.keys(r.branchMap).length > 0 || Object.keys(r.filePathMap).length > 0;
  if (r.destinationTemplate.includes('{env}') && !hasEnvStrategy) {
    problems.push('destination_template uses {env} but no environment strategy is enabled');
  }
  return problems;
}

/** Rules live in Git, so "new rule" writes the CUE to add to config.cue. */
export function NewRulePanel({ open, onClose, bundle, existing }: { open: boolean; onClose: () => void; bundle?: BundleView; existing: string[] }) {
  const [d, setD] = useState<Draft>(EMPTY);
  useEffect(() => {
    if (open) setD({ ...EMPTY, datasource: bundle?.datasources[0]?.name ?? '', platform: bundle?.platforms[0]?.name ?? 'github' });
  }, [open]);

  const rule = draftToRule(d, bundle);
  const problems = ruleProblems(rule);
  if (existing.includes(rule.name)) problems.push(`a rule for ${rule.name} already exists — rules are matched by platform and datasource`);
  const cue = toCue(rule);

  const text = (key: keyof Draft, props: Record<string, unknown> = {}) => ({
    value: d[key] as string,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => setD(x => ({ ...x, [key]: e.target.value })),
    ...props,
  });
  const check = (key: keyof Draft) => ({
    checked: d[key] as boolean,
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setD(x => ({ ...x, [key]: e.target.checked })),
  });

  return (
    <SlideOver
      open={open}
      onClose={onClose}
      wide
      title="New rule"
      subtitle={<>Rules are part of the bundle. Fill this in, then add the generated CUE to <code>rules</code> in <code>config.cue</code> and push.</>}
      actions={
        <>
          <CopyButton text={cue} label="Copy CUE" />
          <span className="secondary" style={{ fontSize: 12.5 }}>Copy CUE</span>
          <button className="btn" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      <div className="grid-2" style={{ alignItems: 'start' }}>
        <div className="stack">
          <h3 className="section-label">General</h3>
          <div className="fields">
            <label className="field">
              <span className="field__label">Platform</span>
              {bundle?.platforms.length ? (
                <select className="select" {...text('platform')}>
                  {bundle.platforms.map(p => <option key={p.name}>{p.name}</option>)}
                </select>
              ) : (
                <input className="input mono" {...text('platform')} />
              )}
            </label>
            <label className="field">
              <span className="field__label">Datasource</span>
              {bundle?.datasources.length ? (
                <select className="select" {...text('datasource')}>
                  {bundle.datasources.map(x => <option key={x.name}>{x.name}</option>)}
                </select>
              ) : (
                <input className="input mono" {...text('datasource')} />
              )}
            </label>
          </div>
          <h3 className="section-label">Source</h3>
          <label className="field">
            <span className="field__label">Repositories</span>
            <input className="input mono" placeholder="my-org/*, my-org/api" {...text('repositories')} />
            <span className="field__hint">Glob patterns, comma separated.</span>
          </label>
          <label className="field">
            <span className="field__label">Exclude repositories</span>
            <input className="input mono" placeholder="my-org/sandbox-*" {...text('excludeRepositories')} />
          </label>
          <label className="field">
            <span className="field__label">File</span>
            <input className="input mono" {...text('fileName')} />
          </label>
          <h3 className="section-label">Environments</h3>
          <label className="field">
            <span className="field__label">Branch map</span>
            <textarea className="textarea" style={{ minHeight: 70 }} {...text('branchMap')} />
            <span className="field__hint">One <code>branch=environment</code> per line.</span>
          </label>
          <div>
            <label className="check"><input type="checkbox" {...check('prAsEnv')} /> <span>Pull request #N becomes environment pr-N</span></label>
            <label className="check"><input type="checkbox" {...check('branchAsEnv')} /> <span>Branch name is the environment</span></label>
            <label className="check"><input type="checkbox" {...check('tagAsEnv')} /> <span>Tag name is the environment</span></label>
          </div>
          <h3 className="section-label">Destination</h3>
          <label className="field">
            <span className="field__label">Destination template</span>
            <input className="input mono" {...text('destinationTemplate')} />
            <span className="field__hint">
              <code>{'{env}'}</code> and <code>{'{tenant}'}</code> are filled in at sync time.
            </span>
          </label>
          <h3 className="section-label">Sync policy</h3>
          <label className="field">
            <span className="field__label">Sync mode</span>
            <select className="select" {...text('syncMode')}>
              {['AUTO', 'GIT_UPSERT_ALL', 'GIT_SMART_REPAIR', 'LIVE_STATE'].map(m => <option key={m}>{m}</option>)}
            </select>
          </label>
          <div>
            <label className="check"><input type="checkbox" {...check('selfHeal')} /> <span>Self heal — restore drift from Git automatically</span></label>
            <label className="check"><input type="checkbox" {...check('prune')} /> <span>Prune keys no longer in Git</span></label>
          </div>
          {d.prune && (
            <label className="field">
              <span className="field__label">Protect from pruning</span>
              <input className="input mono" placeholder="secrets.*, feature_flags.*" {...text('protection')} />
            </label>
          )}
          {!!bundle?.secretManagers.length && (
            <label className="field">
              <span className="field__label">Secret manager</span>
              <select className="select" {...text('secretManager')}>
                <option value="">none</option>
                {bundle.secretManagers.map(s => <option key={s.name}>{s.name}</option>)}
              </select>
            </label>
          )}
        </div>
        <div className="stack" style={{ position: 'sticky', top: 0 }}>
          <h3 className="section-label">config.cue</h3>
          <pre className="code">{cue}</pre>
          {problems.length ? (
            <Notice tone="warning">
              <strong>Not valid yet:</strong>
              <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                {problems.map(p => <li key={p}>{p}</li>)}
              </ul>
            </Notice>
          ) : (
            <Notice tone="good">Passes the bundle's rule checks.</Notice>
          )}
        </div>
      </div>
    </SlideOver>
  );
}
