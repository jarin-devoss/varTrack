import type { RuleView } from '../api/types';
import { targetSync } from './status';

export const FACET_KEYS = ['status', 'drift', 'sync', 'sink', 'env', 'platform', 'selfHeal'] as const;
export type FacetKey = (typeof FACET_KEYS)[number];
export type Facets = Record<FacetKey, string[]>;

export const EMPTY_FACETS: Facets = { status: [], drift: [], sync: [], sink: [], env: [], platform: [], selfHeal: [] };

// Sync status, drift and last sync match per environment: "OutOfSync" finds
// every rule with at least one environment out of sync, which is what the
// drill-down from the overview needs. A rule can count under several values.
const VALUES: Record<FacetKey, (r: RuleView) => string[]> = {
  status: r => r.targets.map(targetSync),
  drift: r => r.targets.map(t => t.drift),
  sync: r => r.targets.map(t => t.lastSync?.state ?? 'never'),
  sink: r => [r.sinkType],
  env: r => r.targets.map(t => t.env),
  platform: r => [r.platform],
  selfHeal: r => [r.selfHeal ? 'on' : 'off'],
};

export function facetValues(rule: RuleView, key: FacetKey): string[] {
  return VALUES[key](rule);
}

export function matchesSearch(rule: RuleView, search: string): boolean {
  const q = search.trim().toLowerCase();
  if (!q) return true;
  const haystack = [
    rule.name,
    rule.fileName ?? '',
    rule.destinationTemplate,
    ...rule.repositories,
    ...Object.keys(rule.filePathMap),
    ...rule.targets.flatMap(t => [t.env, t.destination, t.repository]),
  ];
  return haystack.some(h => h.toLowerCase().includes(q));
}

function matchesFacet(rule: RuleView, key: FacetKey, selected: string[]): boolean {
  return selected.length === 0 || VALUES[key](rule).some(v => selected.includes(v));
}

export function filterRules(rules: RuleView[], facets: Facets, search: string): RuleView[] {
  return rules.filter(r => matchesSearch(r, search) && FACET_KEYS.every(k => matchesFacet(r, k, facets[k])));
}

/**
 * Count, for each value of one facet, the rules that match the search and
 * every *other* facet. Ignoring the facet's own selection keeps the
 * alternatives visible: picking "Drifted" doesn't zero out "In sync".
 */
export function facetCounts(rules: RuleView[], facets: Facets, search: string, key: FacetKey): Map<string, number> {
  const counts = new Map<string, number>();
  for (const rule of rules) {
    if (!matchesSearch(rule, search)) continue;
    if (!FACET_KEYS.every(k => k === key || matchesFacet(rule, k, facets[k]))) continue;
    for (const v of new Set(VALUES[key](rule))) counts.set(v, (counts.get(v) ?? 0) + 1);
  }
  return counts;
}

export function activeFacetCount(facets: Facets): number {
  return FACET_KEYS.reduce((n, k) => n + facets[k].length, 0);
}

export function facetsFromParams(params: URLSearchParams): Facets {
  const facets = { ...EMPTY_FACETS };
  for (const key of FACET_KEYS) {
    const raw = params.get(key);
    facets[key] = raw ? raw.split(',').filter(Boolean) : [];
  }
  return facets;
}

export function writeFacets(params: URLSearchParams, facets: Facets): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of FACET_KEYS) {
    if (facets[key].length) next.set(key, facets[key].join(','));
    else next.delete(key);
  }
  return next;
}
