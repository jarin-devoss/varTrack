import type { RuleView, Target } from '../../api/types';
import { StatusIcon } from '../../components/Status';

/** Which step of the resolution order produced this environment. */
export function resolvedVia(rule: RuleView, t: Target): string {
  if (rule.branchMap[t.ref] === t.env) return `branch_map: ${t.ref} → ${t.env}`;
  const pattern = Object.entries(rule.filePathMap).find(([, env]) => env === t.env)?.[0];
  if (pattern) return `file_path_map: ${pattern}`;
  if (rule.prAsEnv && /^pr-\d+$/.test(t.env)) return 'pull request number';
  if (rule.branchAsEnv && t.env === t.ref) return 'branch name';
  if (rule.tagAsEnv) return 'tag name';
  return 'default';
}

function steps(rule: RuleView) {
  return [
    {
      name: 'branch_map',
      description: 'explicit branch → environment mapping',
      active: Object.keys(rule.branchMap).length > 0,
      entries: Object.entries(rule.branchMap).map(([b, e]) => `${b} → ${e}`),
    },
    {
      name: 'file_path_map',
      description: 'glob on the changed file path',
      active: Object.keys(rule.filePathMap).length > 0,
      entries: Object.entries(rule.filePathMap).map(([p, e]) => `${p} → ${e}`),
    },
    { name: 'pr_as_env', description: 'pull request #N becomes pr-N', active: rule.prAsEnv, entries: [] },
    { name: 'branch_as_env', description: 'the branch name is the environment', active: rule.branchAsEnv, entries: [] },
    { name: 'tag_as_env', description: 'the tag name is the environment', active: rule.tagAsEnv, entries: [] },
    { name: 'default', description: 'fallback when nothing else matches', active: true, entries: [] },
  ];
}

/** The order varTrack tries when it picks an environment for a push. */
export function ResolutionSteps({ rule }: { rule: RuleView }) {
  return (
    <ol style={{ margin: 0, padding: 0, listStyle: 'none' }} className="stack stack--tight">
      {steps(rule).map((step, i) => (
        <li key={step.name} className="row row--wrap" style={{ alignItems: 'flex-start', opacity: step.active ? 1 : 0.55 }}>
          <span className="num muted" style={{ width: 18 }}>
            {i + 1}.
          </span>
          <StatusIcon icon={step.active ? 'checkCircle' : 'minusCircle'} tone={step.active ? 'good' : 'neutral'} size={15} />
          <span style={{ flex: 1, minWidth: 200 }}>
            <code>{step.name}</code> <span className="muted">— {step.description}</span>
            {!step.active && <span className="faint"> (not configured)</span>}
            {step.active && step.entries.length > 0 && (
              <span className="row row--wrap" style={{ gap: 6, marginTop: 4 }}>
                {step.entries.map(e => (
                  <span key={e} className="tag mono">
                    {e}
                  </span>
                ))}
              </span>
            )}
          </span>
        </li>
      ))}
    </ol>
  );
}
