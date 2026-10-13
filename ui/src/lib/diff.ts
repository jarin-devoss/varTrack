import type { KeyEntry } from '../api/types';

export type ChangeKind = 'same' | 'changed' | 'onlyInSink' | 'missing';

export interface KeyChange {
  key: string;
  kind: ChangeKind;
  /** Value in the sink. */
  live?: string;
  /** Value in Git. */
  git?: string;
}

export function keyChanges(keys: KeyEntry[]): KeyChange[] {
  return keys
    .map(k => {
      const kind: ChangeKind =
        k.state === 'unmanaged' ? 'onlyInSink' : k.state === 'missing' ? 'missing' : k.state === 'drifted' ? 'changed' : 'same';
      return { key: k.key, kind, live: k.liveValue, git: k.gitValue };
    })
    .sort((a, b) => a.key.localeCompare(b.key));
}

export function diffCounts(changes: KeyChange[]) {
  return {
    changed: changes.filter(c => c.kind === 'changed').length,
    onlyInSink: changes.filter(c => c.kind === 'onlyInSink').length,
    missing: changes.filter(c => c.kind === 'missing').length,
  };
}

export function hasDifferences(changes: KeyChange[]): boolean {
  return changes.some(c => c.kind !== 'same');
}

export interface DiffLine {
  sign: ' ' | '-' | '+';
  key: string;
  text: string;
}

/**
 * Unified diff of the sink (−) against Git (+), one `key: value` per line.
 * Compact mode drops the unchanged keys.
 */
export function unifiedLines(changes: KeyChange[], compact: boolean): DiffLine[] {
  const lines: DiffLine[] = [];
  for (const c of changes) {
    if (c.kind === 'same') {
      if (!compact) lines.push({ sign: ' ', key: c.key, text: `${c.key}: ${c.git ?? ''}` });
      continue;
    }
    if (c.live !== undefined) lines.push({ sign: '-', key: c.key, text: `${c.key}: ${c.live}` });
    if (c.git !== undefined) lines.push({ sign: '+', key: c.key, text: `${c.key}: ${c.git}` });
  }
  return lines;
}

/** Plain-text form, for copying into a ticket or chat. */
export function unifiedText(title: string, changes: KeyChange[]): string {
  const body = unifiedLines(changes, true).map(l => `${l.sign} ${l.text}`);
  return [`--- ${title} (sink)`, `+++ ${title} (git)`, ...body].join('\n');
}
