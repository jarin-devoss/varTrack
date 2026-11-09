import type { RuleLike } from '../lib/rules';

const segments = (name: string) => name.split('/').map(encodeURIComponent).join('/');

/**
 * A rule page. With the tenant it is `/rules/{tenant}/{platform}/{datasource}`;
 * a bare `platform/datasource` still opens the rule when one tenant has it, and
 * lists the tenants to choose from when several do.
 */
export function ruleHref(rule: string | RuleLike, query = ''): string {
  const { name, tenant } = typeof rule === 'string' ? { name: rule, tenant: undefined } : rule;
  return `/rules/${tenant ? `${encodeURIComponent(tenant)}/` : ''}${segments(name)}${query}`;
}

/** A rule page with one environment's panel open, optionally on a given view. */
export function envHref(rule: string | RuleLike, env: string, view?: string): string {
  return ruleHref(rule, `?env=${encodeURIComponent(env)}${view ? `&view=${view}` : ''}`);
}

/** A rule page with the log of one dry run open over it (a slide-over; the link can be shared). */
export function runLogHref(rule: string | RuleLike, taskId: string): string {
  return ruleHref(rule, `?run=${encodeURIComponent(taskId)}`);
}

export function taskHref(taskId: string): string {
  return `/tasks/${encodeURIComponent(taskId)}`;
}
