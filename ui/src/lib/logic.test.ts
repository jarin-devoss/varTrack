import { describe, expect, it } from 'vitest';
import { mapFileResult, mapTaskState } from '../api/live';
import type { RuleView } from '../api/types';
import { toCue } from '../features/rule/DefinitionTab';
import { resolvedVia } from '../features/rule/resolution';
import { taskStages } from '../features/tasks';
import { makeRule, makeSync, makeTarget as target, makeTask } from '../test/builders';
import { EMPTY_FACETS, facetCounts, facetsFromParams, filterRules, writeFacets } from './filters';
import { needsAttention, ruleSync, ruleSyncState, targetNote, targetSync, worstDrift } from './status';
import { formatDuration, relativeTime } from './time';

const NOW = Date.parse('2026-10-11T12:00:00Z');

// A small tenant: one rule per situation the screens have to handle.
const mongoDr = makeRule({ datasource: 'mongo-dr', targets: [target({ drift: 'drifted', driftedKeys: 2 })] });
const mongoPrimary = makeRule({
  datasource: 'mongo-primary',
  envAsPr: true,
  targets: [target(), target({ env: 'pr-412', ref: 'pr-412', destination: 'pr-412-config' })],
});
const zookeeper = makeRule({
  datasource: 'zookeeper',
  sinkType: 'zookeeper',
  targets: [target({ env: 'staging', ref: 'develop', drift: 'heal_failed', driftedKeys: 2, lastSync: makeSync({ state: 'FAILED', trigger: 'self_heal' }) })],
});
const s3 = makeRule({ datasource: 's3', sinkType: 's3', targets: [target({ env: 'staging', ref: 'develop', lastSync: makeSync({ state: 'FAILED', error: 'AccessDenied' }) })] });
const linux = makeRule({ datasource: 'linux_server', sinkType: 'linux_server', repositories: ['acme/legacy-billing'], targets: [target({ drift: 'unknown' })] });
const redis = makeRule({ datasource: 'redis', sinkType: 'redis', targets: [target()] });
const rules = [mongoDr, mongoPrimary, zookeeper, s3, linux, redis];

describe('status roll-ups', () => {
  it('takes the worst drift across environments', () => {
    expect(worstDrift(['in_sync', 'healing', 'not_watched'])).toBe('healing');
    expect(worstDrift(['in_sync', 'heal_failed', 'drifted'])).toBe('heal_failed');
    expect(worstDrift(['not_watched'])).toBe('not_watched');
    expect(worstDrift([])).toBe('not_watched');
  });

  it('reports a running sync before an older failure', () => {
    const r: RuleView = makeRule({
      targets: [
        target({ lastSync: makeSync({ taskId: 'a', state: 'FAILED', startedAt: '2026-10-11T10:00:00Z' }) }),
        target({ env: 'staging', lastSync: makeSync({ taskId: 'b', state: 'RUNNING', trigger: 'ui', startedAt: '2026-10-11T11:00:00Z' }) }),
      ],
    });
    expect(ruleSyncState(r)).toBe('RUNNING');
    expect(ruleSyncState({ ...r, targets: [r.targets[0]] })).toBe('FAILED');
    expect(ruleSyncState({ ...r, targets: [] })).toBe('never');
  });

  it('lists what needs attention, worst first', () => {
    const items = needsAttention(rules);
    expect(items[0].target.drift).toBe('heal_failed');
    const keys = items.map(i => `${i.rule.name}:${i.target.env}:${i.kind}`);
    expect(keys).toContain('github/mongo-dr:production:drift');
    expect(keys).toContain('github/linux_server:production:drift');
    expect(keys).toContain('github/s3:staging:sync');
    // A failed heal is reported once, as drift, not again as a failed sync.
    expect(keys.filter(k => k.startsWith('github/zookeeper:staging'))).toEqual(['github/zookeeper:staging:drift']);
  });
});

describe('current vs desired', () => {
  it('calls an environment OutOfSync while it differs from Git, healing or not', () => {
    expect(targetSync(target({ drift: 'in_sync' }))).toBe('Synced');
    for (const drift of ['drifted', 'healing', 'heal_failed'] as const) expect(targetSync(target({ drift }))).toBe('OutOfSync');
    expect(targetSync(target({ drift: 'unknown' }))).toBe('Unknown');
    expect(targetSync(target({ drift: 'not_watched' }))).toBe('Unknown');
  });

  it('rolls environments up to the rule, out of sync first', () => {
    expect(ruleSync(makeRule({ targets: [target({ drift: 'in_sync' }), target({ drift: 'drifted' })] }))).toBe('OutOfSync');
    expect(ruleSync(makeRule({ targets: [target({ drift: 'in_sync' }), target({ drift: 'not_watched' })] }))).toBe('Synced');
    expect(ruleSync(makeRule({ targets: [target({ drift: 'not_watched' })] }))).toBe('Unknown');
    expect(ruleSync(mongoDr)).toBe('OutOfSync');
  });

  it('says what is going on beyond the sync status', () => {
    expect(targetNote(target({ drift: 'drifted', driftedKeys: 1 }))).toBe('1 key differs');
    expect(targetNote(target({ drift: 'drifted', driftedKeys: 3 }))).toBe('3 keys differ');
    expect(targetNote(target({ drift: 'heal_failed' }))).toBe('self-heal gave up');
    expect(targetNote(target({ drift: 'in_sync' }))).toBe('matches Git');
    expect(targetNote(target({ drift: 'not_watched' }))).toBe('not watched');
  });
});

describe('facet filtering', () => {
  it('filters by sync status per environment', () => {
    const out = filterRules(rules, { ...EMPTY_FACETS, status: ['OutOfSync'] }, '').map(r => r.name);
    expect(out).toContain('github/mongo-dr');
    expect(out).not.toContain('github/redis');
  });

  it('matches drift per environment and searches repos and destinations', () => {
    expect(filterRules(rules, { ...EMPTY_FACETS, drift: ['drifted'] }, '').map(r => r.name)).toEqual(['github/mongo-dr']);
    expect(filterRules(rules, EMPTY_FACETS, 'legacy-billing').map(r => r.name)).toEqual(['github/linux_server']);
    expect(filterRules(rules, EMPTY_FACETS, 'pr-412-config').map(r => r.name)).toEqual(['github/mongo-primary']);
  });

  it('counts each option against the other facets only', () => {
    const facets = { ...EMPTY_FACETS, sink: ['mongo'], drift: ['in_sync'] };
    const driftCounts = facetCounts(rules, facets, '', 'drift');
    // Both mongo rules are counted, even though only one has an in-sync env.
    expect(driftCounts.get('in_sync')).toBe(1);
    expect(driftCounts.get('drifted')).toBe(1);
    const sinkCounts = facetCounts(rules, facets, '', 'sink');
    expect(sinkCounts.get('mongo')).toBe(1);
    expect(sinkCounts.get('redis')).toBe(1);
  });

  it('round-trips facets through the URL', () => {
    const facets = { ...EMPTY_FACETS, drift: ['drifted', 'heal_failed'], sink: ['s3'] };
    const params = writeFacets(new URLSearchParams('q=abc'), facets);
    expect(params.get('q')).toBe('abc');
    expect(facetsFromParams(params)).toEqual(facets);
  });
});

describe('live API mapping', () => {
  it('maps Celery states, preferring the result state', () => {
    expect(mapTaskState('PENDING')).toBe('PENDING');
    expect(mapTaskState('STARTED')).toBe('RUNNING');
    expect(mapTaskState('PROGRESS')).toBe('RUNNING');
    expect(mapTaskState('RETRY')).toBe('RETRYING');
    expect(mapTaskState('SUCCESS')).toBe('SUCCEEDED');
    expect(mapTaskState('SUCCESS', 'FAILED')).toBe('FAILED');
  });

  it('maps sync and dry-run file results', () => {
    expect(mapFileResult({ file: 'a.yaml', env: 'prod', status: 'validation_failed', sync_mode: 'GIT_UPSERT_ALL', written: 0, pruned: 0, error: 'bad' })).toMatchObject({
      file: 'a.yaml',
      status: 'validation_failed',
      syncMode: 'GIT_UPSERT_ALL',
      error: 'bad',
    });
    expect(mapFileResult({ file: 'a.yaml', env: 'prod', keys: 12, validation: 'ok', sample: { a: '1' } })).toMatchObject({
      status: 'ok',
      keys: 12,
      sample: { a: '1' },
    });
  });
});

describe('rule definition', () => {
  it('renders the rule as CUE with only configured fields', () => {
    const cue = toCue(makeRule({ prune: { enabled: true, last: false, dryRun: false, protection: ['feature_flags.*', 'secrets.*'] } }));
    expect(cue).toContain('platform:             "github"');
    expect(cue).toMatch(/branch_map:\s+\{/);
    expect(cue).toContain('main:    "production"');
    expect(cue).toContain('protection: ["feature_flags.*", "secrets.*"]');
    expect(cue).not.toContain('env_as_branch');
    const byPath = makeRule({ fileName: undefined, branchMap: {}, filePathMap: { 'deploy/prod/*.yaml': 'production' } });
    expect(toCue(byPath)).toMatch(/"deploy\/prod\/\*\.yaml":\s+"production"/);
  });

  it('explains how each environment was resolved', () => {
    expect(resolvedVia(mongoPrimary, mongoPrimary.targets[0])).toBe('branch_map: main → production');
    expect(resolvedVia(mongoPrimary, mongoPrimary.targets[1])).toBe('pull request number');
    const byPath = makeRule({ branchMap: {}, filePathMap: { 'deploy/prod/*.yaml': 'production' } });
    expect(resolvedVia(byPath, target())).toBe('file_path_map: deploy/prod/*.yaml');
    const byBranch = makeRule({ branchMap: {}, envAsBranch: true });
    expect(resolvedVia(byBranch, target({ env: 'main', ref: 'main' }))).toBe('branch name');
  });
});

describe('task stages', () => {
  const base = makeTask();
  it('marks the failing stage', () => {
    const invalid = { ...base, state: 'FAILED' as const, files: [{ ...base.files[0], status: 'validation_failed' as const }] };
    expect(taskStages(invalid).map(s => s.state)).toEqual(['done', 'failed', 'skipped']);
    const writeFailed = { ...base, state: 'FAILED' as const, files: [{ ...base.files[0], status: 'error' as const }] };
    expect(taskStages(writeFailed).map(s => s.state)).toEqual(['done', 'done', 'failed']);
    expect(taskStages({ ...base, state: 'PENDING', files: [] }).map(s => s.state)).toEqual(['active', 'pending', 'pending']);
  });
});

describe('time formatting', () => {
  it('formats relative times and durations', () => {
    expect(relativeTime('2026-10-11T11:59:55Z', NOW)).toBe('just now');
    expect(relativeTime('2026-10-11T11:59:00Z', NOW)).toBe('1m ago');
    expect(relativeTime('2026-10-11T09:00:00Z', NOW)).toBe('3h ago');
    expect(relativeTime(undefined, NOW)).toBe('—');
    expect(formatDuration('2026-10-11T12:00:00Z', '2026-10-11T12:00:02.500Z')).toBe('2.5s');
    expect(formatDuration('2026-10-11T12:00:00Z', '2026-10-11T12:03:04Z')).toBe('3m 04s');
  });
});
