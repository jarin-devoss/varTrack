export function ruleHref(rule: string, query = ''): string {
  return `/rules/${rule.split('/').map(encodeURIComponent).join('/')}${query}`;
}

/** A rule page with one environment's panel open, optionally on a given view. */
export function envHref(rule: string, env: string, view?: string): string {
  return ruleHref(rule, `?env=${encodeURIComponent(env)}${view ? `&view=${view}` : ''}`);
}

export function taskHref(taskId: string): string {
  return `/tasks/${encodeURIComponent(taskId)}`;
}
