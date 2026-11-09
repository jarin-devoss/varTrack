import type { VarTrackApi } from '../api';
import type { Task } from '../api/types';
import { isActive } from '../lib/status';

/**
 * Ask about the tasks a sync started until they have all finished (a sync takes well under a second; a minute at most is
 * waited). Returns the tasks as they last were, and whether they all ended. Without this the page would show the sync
 * running until its next poll, seconds later, and nothing would say how it went.
 */
export async function untilFinished(api: VarTrackApi, taskIds: string[], limitMs = 60_000): Promise<{ tasks: Task[]; ended: boolean }> {
  const deadline = Date.now() + limitMs;
  const last = new Map<string, Task>();
  let pending = [...taskIds];
  for (let wait = 150; pending.length > 0 && Date.now() < deadline; wait = Math.min(wait * 2, 2000)) {
    await new Promise(resolve => setTimeout(resolve, wait));
    const found = await Promise.all(pending.map(id => api.getTask(id).catch(() => undefined)));
    found.forEach((task, i) => task && last.set(pending[i], task));
    pending = pending.filter(id => {
      const task = last.get(id);
      return !task || isActive(task.state);
    });
  }
  return { tasks: taskIds.flatMap(id => last.get(id) ?? []), ended: pending.length === 0 };
}
