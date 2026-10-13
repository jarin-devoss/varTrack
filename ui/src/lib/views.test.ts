import { describe, expect, it } from 'vitest';
import type { Task, VtEvent } from '../api/types';
import { groupEvents } from '../features/events';
import { activeSpans, initiatedBy } from '../features/rule/HistoryTimeline';
import { makeKey, makeRule, makeSync, makeTarget } from '../test/builders';
import { diffCounts, hasDifferences, keyChanges, unifiedLines, unifiedText } from './diff';

describe('diff', () => {
  const keys = [
    makeKey('app.name', 'checkout', 'checkout'),
    makeKey('app.region', 'us-west-2', 'us-west-2-hotfix'),
    makeKey('database.host', 'mongo.dr.internal', 'mongo.dr.internal-hotfix'),
    makeKey('cache.ttl', '300', '300'),
  ];
  const changes = keyChanges(keys);

  it('classifies keys and counts the differences', () => {
    expect(hasDifferences(changes)).toBe(true);
    expect(diffCounts(changes)).toEqual({ changed: 2, onlyInSink: 0, missing: 0 });
    expect(hasDifferences(keyChanges([makeKey('a', '1', '1'), makeKey('b', '2', '2')]))).toBe(false);
  });

  it('writes the sink as − and Git as +, dropping unchanged keys when compact', () => {
    const compact = unifiedLines(changes, true);
    expect(compact).toHaveLength(4);
    expect(compact[0]).toMatchObject({ sign: '-', key: 'app.region', text: 'app.region: us-west-2-hotfix' });
    expect(compact[1]).toMatchObject({ sign: '+', key: 'app.region', text: 'app.region: us-west-2' });
    expect(unifiedLines(changes, false).filter(l => l.sign === ' ').length).toBe(keys.length - 2);
  });

  it('shows keys only in the sink or only in Git on one side', () => {
    const staging = keyChanges([makeKey('app.name', 'checkout', 'checkout'), makeKey('legacy.cart_v1', undefined, 'true')]);
    expect(unifiedLines(staging, true)).toEqual([{ sign: '-', key: 'legacy.cart_v1', text: 'legacy.cart_v1: true' }]);
    const zk = keyChanges([makeKey('fraud.review_queue', 'manual', undefined)]);
    expect(unifiedLines(zk, true).some(l => l.sign === '+' && l.key === 'fraud.review_queue')).toBe(true);
    expect(unifiedText('x', staging).split('\n')[0]).toBe('--- x (sink)');
  });
});

describe('history', () => {
  const task = (id: string, env: string, at: string, extra: Partial<Task> = {}): Task => ({
    taskId: id,
    state: 'SUCCEEDED',
    message: '',
    trigger: 'webhook',
    datasource: 'mongo',
    env,
    dryRun: false,
    createdAt: at,
    finishedAt: at,
    files: [],
    ...extra,
  });

  it('measures how long each write stayed live, per environment', () => {
    const spans = activeSpans([
      task('a', 'prod', '2026-10-11T10:00:00Z'),
      task('b', 'staging', '2026-10-11T10:30:00Z'),
      task('c', 'prod', '2026-10-11T11:00:00Z'),
      task('d', 'prod', '2026-10-11T11:30:00Z', { dryRun: true }),
      task('e', 'prod', '2026-10-11T11:45:00Z', { state: 'FAILED' }),
    ]);
    expect(spans.get('a')).toEqual({ until: '2026-10-11T11:00:00Z', current: false });
    expect(spans.get('c')).toEqual({ current: true });
    expect(spans.get('b')).toEqual({ current: true });
    expect(spans.has('d')).toBe(false);
    expect(spans.has('e')).toBe(false);
  });

  it('says who started a sync', () => {
    expect(initiatedBy(task('a', 'p', '', { trigger: 'self_heal' }))).toBe('Automated · self-heal');
    expect(initiatedBy(task('a', 'p', '', { submittedBy: 'dana' }))).toBe('Git push by dana');
  });
});

describe('event grouping', () => {
  const ev = (id: string, time: string, message = 'drift'): VtEvent => ({ id, time, type: 'Warning', reason: 'DriftDetected', rule: 'r', env: 'e', message });

  it('folds repeats into one row with count and first/last seen', () => {
    const groups = groupEvents([ev('1', '2026-10-11T10:00:00Z'), ev('2', '2026-10-11T12:00:00Z'), ev('3', '2026-10-11T11:00:00Z', 'other')]);
    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ count: 2, firstSeen: '2026-10-11T10:00:00Z' });
    expect(groups[0].event.id).toBe('2');
  });
});

describe('headlines', () => {
  const rules = [
    makeRule({ datasource: 'mongo-dr', targets: [makeTarget({ drift: 'drifted', driftedKeys: 2 })] }),
    makeRule({
      datasource: 'zookeeper',
      targets: [makeTarget({ env: 'staging', drift: 'heal_failed', driftedKeys: 2, lastSync: makeSync({ state: 'FAILED' }) })],
    }),
    makeRule({ datasource: 'configmap', targets: [makeTarget(), makeTarget({ env: 'staging' })] }),
    makeRule({ datasource: 'helm', selfHeal: false, targets: [makeTarget({ drift: 'not_watched' })] }),
  ];

  it('summarises the tenant worst first', async () => {
    const { overallStatus } = await import('../pages/DashboardPage');
    expect(overallStatus(rules)).toMatchObject({ color: 'red', title: 'Action Needed' });
    const calm = rules.map(r => ({ ...r, targets: r.targets.map(t => ({ ...t, drift: 'in_sync' as const, lastSync: t.lastSync && { ...t.lastSync, state: 'SUCCEEDED' as const } })) }));
    expect(overallStatus(calm)).toMatchObject({ color: 'green' });
    expect(overallStatus([])).toMatchObject({ color: 'gray' });
  });

  it('says what matters most about a rule', async () => {
    const { ruleMessage } = await import('../features/ruleViews');
    const byName = (n: string) => rules.find(r => r.name === n)!;
    expect(ruleMessage(byName('github/mongo-dr'))).toBe('production: 2 keys differ from Git');
    expect(ruleMessage(byName('github/zookeeper'))).toBe('staging: 2 keys differ from Git');
    expect(ruleMessage(byName('github/configmap'))).toBe('All environments match Git');
    expect(ruleMessage(byName('github/helm'))).toMatch(/not watched/);
  });
});

describe('token identity', () => {
  it('reads display claims without verifying', async () => {
    const { tokenIdentity } = await import('./token');
    const payload = btoa(JSON.stringify({ sub: 'u1', preferred_username: 'dana', iss: 'https://sso', groups: ['ops'], exp: 2000000000 })).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    expect(tokenIdentity(`h.${payload}.s`)).toMatchObject({ name: 'dana', issuer: 'https://sso', groups: ['ops'] });
    expect(tokenIdentity('not-a-jwt')).toBeNull();
  });
});
