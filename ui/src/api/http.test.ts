import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, isNotAvailable, requestJson } from './http';

/** What the orchestrator answers is what the UI makes of it: a 404 that carries an answer is not a missing route. */
function answer(status: number, body: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })));
}

afterEach(() => vi.unstubAllGlobals());

describe('a 404', () => {
  it('that FastAPI gives a route it does not have is "not available on this server"', async () => {
    answer(404, { detail: 'Not Found' });
    const error = await requestJson('/api/orchestrator/v1/ui/rules/x').catch(e => e);
    expect(isNotAvailable(error)).toBe(true);
  });

  it('that says what is missing is that answer, so the page can say "no rule named …"', async () => {
    answer(404, { detail: 'no rule gitea/redis-hash' });
    const error = await requestJson('/api/orchestrator/v1/ui/rules/gitea/redis-hash').catch(e => e);
    expect(isNotAvailable(error)).toBe(false);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(404);
    expect(error.message).toBe('no rule gitea/redis-hash');
  });

  it('with no body at all stays "not available" (a proxy in front of an older server)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })));
    expect(isNotAvailable(await requestJson('/x').catch(e => e))).toBe(true);
  });

  it('is still accepted when the caller asks for it', async () => {
    answer(404, { detail: 'no such task' });
    const { status } = await requestJson('/x', { acceptStatus: [404] });
    expect(status).toBe(404);
  });
});

describe('other statuses', () => {
  it('405 and 501 stay "not available"', async () => {
    for (const status of [405, 501]) {
      answer(status, { detail: 'nope' });
      expect(isNotAvailable(await requestJson('/x').catch(e => e))).toBe(true);
    }
  });

  it('a 403 carries its reason', async () => {
    answer(403, { detail: 'permission denied: wes may not push to main' });
    const error = await requestJson('/x').catch(e => e);
    expect(error.status).toBe(403);
    expect(error.message).toBe('permission denied: wes may not push to main');
  });
});
