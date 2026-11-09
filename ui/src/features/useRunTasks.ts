import { useEffect, useState } from 'react';
import { useApi } from '../api';
import type { Task } from '../api/types';
import { isActive } from '../lib/status';

/**
 * The tasks of a run, as they last were, asked about every second until they have all ended (a dry run takes well under
 * a second; the log fills in as the tasks move). `ended` is true once every task has been seen finished, or after
 * *limitMs* (the log then says what it has).
 */
export function useRunTasks(taskIds: string[] | null, limitMs = 60_000): { tasks: Task[]; ended: boolean } {
  const api = useApi();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [ended, setEnded] = useState(false);
  const key = taskIds?.join(',') ?? '';

  useEffect(() => {
    setTasks([]);
    setEnded(false);
    if (!taskIds?.length) return;
    let live = true;
    const last = new Map<string, Task>();
    const deadline = Date.now() + limitMs;
    void (async () => {
      let pending = [...taskIds];
      while (live && pending.length > 0 && Date.now() < deadline) {
        const found = await Promise.all(pending.map(id => api.getTask(id).catch(() => undefined)));
        if (!live) return;
        found.forEach((task, i) => task && last.set(pending[i], task));
        setTasks(taskIds.flatMap(id => last.get(id) ?? []));
        pending = pending.filter(id => {
          const task = last.get(id);
          return !task || isActive(task.state);
        });
        if (pending.length > 0) await new Promise(resolve => setTimeout(resolve, 1000));
      }
      if (live) setEnded(true);
    })();
    return () => {
      live = false;
    };
  }, [key]);

  return { tasks, ended };
}
