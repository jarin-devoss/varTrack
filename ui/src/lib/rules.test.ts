import { describe, expect, it } from 'vitest';
import { ruleHref, envHref } from '../features/links';
import { makeRule, makeTarget } from '../test/builders';
import { EMPTY_FACETS, facetCounts, filterRules, matchesSearch } from './filters';
import { isPinned, ruleKey, tenantsOf } from './rules';

// The same platform/datasource can exist in two tenants, so a rule is identified by all three.
const acme = makeRule({ tenant: 'acme', targets: [makeTarget({ env: 'production' })] });
const corp = makeRule({ tenant: 'corp', targets: [makeTarget({ env: 'staging' })] });
const other = makeRule({ tenant: 'corp', platform: 'gitea', datasource: 's3' });

describe('rule identity', () => {
  it('keys a rule by tenant, platform and datasource', () => {
    expect(ruleKey(acme)).toBe('acme/github/mongo');
    expect(ruleKey(corp)).toBe('corp/github/mongo');
    expect(ruleKey({ name: 'github/mongo' })).toBe('github/mongo');
  });

  it('lists the tenants a set of rules spans, sorted and once', () => {
    expect(tenantsOf([corp, acme, other])).toEqual(['acme', 'corp']);
    expect(tenantsOf([])).toEqual([]);
  });
});

describe('favorites', () => {
  it('pins one tenant\'s rule without pinning the other tenant\'s rule of the same name', () => {
    const pinned = ['acme/github/mongo'];
    expect(isPinned(pinned, acme)).toBe(true);
    expect(isPinned(pinned, corp)).toBe(false);
  });

  it('keeps a favorite saved before rules had a tenant: it names every rule of that name', () => {
    const pinned = ['github/mongo'];
    expect(isPinned(pinned, acme)).toBe(true);
    expect(isPinned(pinned, corp)).toBe(true);
    expect(isPinned(pinned, other)).toBe(false);
  });
});

describe('rule links', () => {
  it('puts the tenant in the path', () => {
    expect(ruleHref(acme)).toBe('/rules/acme/github/mongo');
    expect(ruleHref(acme, '?tab=diff')).toBe('/rules/acme/github/mongo?tab=diff');
    expect(envHref(corp, 'staging', 'diff')).toBe('/rules/corp/github/mongo?env=staging&view=diff');
  });

  it('keeps a link without a tenant working: the page resolves it', () => {
    expect(ruleHref('github/mongo')).toBe('/rules/github/mongo');
    expect(ruleHref({ name: 'github/mongo' })).toBe('/rules/github/mongo');
  });

  it('escapes what is not path-safe', () => {
    expect(ruleHref({ name: 'github-ent/mongo', tenant: 'team a' })).toBe('/rules/team%20a/github-ent/mongo');
  });
});

describe('tenant filter', () => {
  const rules = [acme, corp, other];

  it('shows every tenant when nothing is chosen', () => {
    expect(filterRules(rules, EMPTY_FACETS, '')).toHaveLength(3);
  });

  it('narrows to the chosen tenant', () => {
    const shown = filterRules(rules, { ...EMPTY_FACETS, tenant: ['corp'] }, '');
    expect(shown.map(ruleKey)).toEqual(['corp/github/mongo', 'corp/gitea/s3']);
  });

  it('counts each tenant\'s rules without letting the tenant choice zero the others out', () => {
    const counts = facetCounts(rules, { ...EMPTY_FACETS, tenant: ['corp'] }, '', 'tenant');
    expect(Object.fromEntries(counts)).toEqual({ acme: 1, corp: 2 });
  });

  it('finds a rule by its tenant in the search box', () => {
    expect(matchesSearch(corp, 'corp')).toBe(true);
    expect(matchesSearch(acme, 'corp')).toBe(false);
  });
});
