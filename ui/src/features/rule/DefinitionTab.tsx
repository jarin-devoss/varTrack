import type { Rule } from '../../api/types';
import { CopyButton, Notice } from '../../components/common';

type CueValue = string | number | boolean | string[] | Record<string, string> | { [k: string]: CueValue };

const quote = (s: string) => JSON.stringify(s);

function render(value: CueValue, indent: string): string {
  if (typeof value === 'string') return quote(value);
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return `[${value.map(quote).join(', ')}]`;
  const entries = Object.entries(value);
  if (entries.length === 0) return '{}';
  const inner = indent + '  ';
  const width = Math.max(...entries.map(([k]) => cueKey(k).length)) + 1;
  const lines = entries.map(([k, v]) => `${inner}${(cueKey(k) + ':').padEnd(width + 1)}${render(v, inner)}`);
  return `{\n${lines.join('\n')}\n${indent}}`;
}

function cueKey(k: string): string {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(k) ? k : quote(k);
}

/** The rule as it would appear in the bundle's `rules` list. */
export function toCue(rule: Rule): string {
  const out: Record<string, CueValue> = {
    platform: rule.platform,
    datasource: rule.datasource,
    repositories: rule.repositories,
  };
  if (rule.excludeRepositories.length) out.exclude_repositories = rule.excludeRepositories;
  if (rule.fileName) out.file_name = rule.fileName;
  if (Object.keys(rule.filePathMap).length) out.file_path_map = rule.filePathMap;
  if (Object.keys(rule.branchMap).length) out.branch_map = rule.branchMap;
  if (rule.prAsEnv) out.pr_as_env = true;
  if (rule.branchAsEnv) out.branch_as_env = true;
  if (rule.tagAsEnv) out.tag_as_env = true;
  out.destination_template = rule.destinationTemplate;
  out.sync_mode = rule.syncMode;
  out.self_heal = rule.selfHeal;
  if (rule.dryRun) out.dry_run = true;
  if (rule.prune.enabled) {
    const prune: Record<string, CueValue> = { enabled: true };
    if (rule.prune.last) prune.last = true;
    if (rule.prune.dryRun) prune.dry_run = true;
    if (rule.prune.protection.length) prune.protection = rule.prune.protection;
    out.prune = prune;
  }
  if (rule.secretManager) out.secret_manager = rule.secretManager;
  return render(out, '');
}

export function DefinitionTab({ rule }: { rule: Rule }) {
  const cue = toCue(rule);
  return (
    <div className="stack">
      <Notice tone="info">
        Rules are part of the CUE bundle, so they change through Git like everything else varTrack manages. Edit the rule in{" "}
        <code>config.cue</code> (or a repo's <code>vartrack.json</code> override) and push.
      </Notice>
      <div className="row">
        <span className="section-label">Rule in config.cue</span>
        <span className="spacer" />
        <CopyButton text={cue} label="Copy CUE" />
      </div>
      <pre className="code">{cue}</pre>
    </div>
  );
}
