import { expect } from '@playwright/test';
import type { UiApi } from './api';
import { CONFIG_REPO, unique, type Gitea } from './gitea';

/** The rule the task fixtures sync: strict validation, and the environment named after its branch. */
export const TASK_RULE = 'gitea/redis-string';
const FILE = 'app-settings.yaml';

/**
 * A task of each kind, made when a spec asks for one, so that a spec reads the task it made and no other (the lists on
 * the Tasks and Events pages are capped, and another spec's leftovers are not this spec's to rely on).
 */
export class Tasks {
  constructor(
    private readonly api: UiApi,
    private readonly gitea: Gitea,
  ) {}

  /** A real sync that works. */
  async ok(): Promise<string> {
    const [id] = await this.api.sync(TASK_RULE, ['develop']);
    await this.api.succeeded(id);
    return id;
  }

  /** A dry run: it runs the pipeline and writes nothing. */
  async dry(): Promise<string> {
    const [id] = await this.api.sync(TASK_RULE, ['develop'], { dryRun: true });
    await this.api.succeeded(id);
    return id;
  }

  /**
   * A sync the rule's strict schema refuses: Git holds an invalid log level for a moment. Returns the task and the invalid
   * value it was refused for.
   */
  async failed(): Promise<{ id: string; bad: string }> {
    const good = (await this.gitea.read(CONFIG_REPO, FILE, 'develop'))!;
    const bad = unique('LOUD');
    try {
      await this.gitea.put(CONFIG_REPO, FILE, good.replace(/log_level: \w+/, `log_level: ${bad}`), 'ui-e2e: an invalid log level', 'develop');
      const [id] = await this.api.sync(TASK_RULE, ['develop']);
      const task = await this.api.finished(id);
      expect(task.state, 'a strict schema refuses the file').toBe('FAILED');
      return { id, bad };
    } finally {
      await this.gitea.put(CONFIG_REPO, FILE, good, 'ui-e2e: put the file back', 'develop');
    }
  }
}
