import type { RuleView } from '../api/types';

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
