import { useEffect, useState } from 'react';
import type { RuleView } from '../api/types';
import { ErrorState, LoadingRows } from '../components/common';
import { SlideOver } from '../components/Overlay';
import { isActive } from '../lib/status';
import { useQuery } from '../lib/useQuery';
import { DryRunReport } from './DryRunReport';

/**
 * The log of one dry run, over the rule's page (`?run=<taskId>`): what was read, how the schema judged it, and what a sync
 * would add, change and delete. It opens from a dry run in the rule's history and from a finished dry run's toast, and the
 * link can be shared. A task that is not this rule's dry run opens nothing.
 */
export function RunLogPanel({ rule, taskId, onClose }: { rule: RuleView; taskId: string | null; onClose: () => void }) {
  const [finished, setFinished] = useState(false);
  const task = useQuery(taskId ? `task:${taskId}` : null, api => api.getTask(taskId!), { poll: finished ? false : 1000 });
  useEffect(() => {
    if (task.data) setFinished(!isActive(task.data.state));
  }, [task.data?.state]);
  const t = task.data;
  const ours = !t || (t.dryRun && (!t.rule || t.rule === rule.name) && (!t.tenant || t.tenant === rule.tenant));
  useEffect(() => {
    if (taskId && t && !ours) onClose();
  }, [taskId, t?.taskId, ours]);

  return (
    <SlideOver open={!!taskId && ours} onClose={onClose} wide title="Dry run log" subtitle={<span className="mono">{rule.name}</span>}>
      {task.error && !t ? <ErrorState compact error={task.error} what="this dry run" onRetry={task.reload} /> : !t ? <LoadingRows rows={3} height={60} /> : <DryRunReport task={t} />}
    </SlideOver>
  );
}
