import type { HiddenRules, RuleView } from '../api/types';
import { tenantsOf } from './rules';
import { useQuery, type QueryState } from './useQuery';

/**
 * Every rule the viewer may see, of every tenant: what Git lets them read, or
 * all of it for an admin. `hidden` counts the rules a connection would add.
 */
export function useRules(opts?: { poll?: number | false }): QueryState<RuleView[]> & { hidden: HiddenRules[] } {
  const query = useQuery('rules', api => api.listRules(), opts);
  return { ...query, data: query.data?.rules, hidden: query.data?.hidden ?? [] };
}

/** The tenants the viewer can see rules of; lists show a Tenant column and filter when there is more than one. */
export function useTenants(): string[] {
  return tenantsOf(useRules().data ?? []);
}
