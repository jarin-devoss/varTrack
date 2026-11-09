import type { KeyEntry, PruneConfig, RuleView, Target } from '../api/types';

/**
 * The environments of a rule that a Sync or Dry run can read: the ones some branch feeds. An environment the rule
 * no longer produces (an earlier version of it wrote it) stays listed, with its keys and history, but is not offered.
 */
export function syncableEnvs(targets: Target[]): string[] {
  return targets.filter(t => !t.notFed).map(t => t.env);
}

/**
 * What the person may start on this rule. The permission engine says whether any environment of it may be synced by them
 * (`canSync` is absent when sign-in is off: everyone may). Someone who may only dry-run is not shown a Sync that is really
 * a dry run: Sync stays, disabled, with `reason`, and Dry run is its own control.
 */
export function runRights(rule: Pick<RuleView, 'canSync'>): { sync: boolean; reason?: string } {
  return rule.canSync === false
    ? { sync: false, reason: "You can read this rule's repository but not write to it, so you can dry-run it but not sync it." }
    : { sync: true };
}

/** The file a sync of the rule reads, in words: its name, its one path, "3 path patterns", or that it names none. */
export function filesRead(rule: Pick<RuleView, 'fileName' | 'filePathMap'>): string {
  if (rule.fileName) return rule.fileName;
  const patterns = Object.keys(rule.filePathMap);
  if (patterns.length === 0) return 'no file named';
  return patterns.length === 1 ? patterns[0] : `${patterns.length} path patterns`;
}

/**
 * Whether a sync of this rule deletes the keys Git no longer has. Pruning is the rule's setting, not a choice made per
 * sync: a rule that prunes does so on every sync that writes, and a dry run only reports what it would delete.
 */
export function prunes(prune: PruneConfig, dryRun: boolean): boolean {
  return prune.enabled && !dryRun;
}

/**
 * Whether a key's value in the sink differs from Git. A key matching the rule's drift_ignore is `ignored` whatever its
 * values are, so it differs only if the values do; any other key that is not `synced` differs (changed, missing, or
 * in the sink but not in Git).
 */
export function keyDiffers(key: Pick<KeyEntry, 'state' | 'gitValue' | 'liveValue'>): boolean {
  if (key.state === 'ignored') return key.liveValue !== key.gitValue;
  return key.state !== 'synced';
}

/** What names a rule: its tenant (when known) and `platform/datasource`. */
export interface RuleLike {
  name: string;
  tenant?: string;
}

/**
 * The identity a rule is kept under in favorites: `tenant/platform/datasource`.
 * `platform/datasource` alone is not unique across tenants.
 */
export function ruleKey(rule: RuleLike): string {
  return rule.tenant ? `${rule.tenant}/${rule.name}` : rule.name;
}

/**
 * Whether a favorite names the rule. Favorites saved before rules had a tenant
 * are `platform/datasource`: they name every rule of that name.
 */
export function isPinned(pinned: string[], rule: RuleLike): boolean {
  return pinned.includes(ruleKey(rule)) || pinned.includes(rule.name);
}

/** The tenants the rules span, sorted. */
export function tenantsOf(rules: Pick<RuleView, 'tenant'>[]): string[] {
  return [...new Set(rules.map(r => r.tenant))].sort();
}
