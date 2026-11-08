import { execFileSync } from 'node:child_process';
import { expect } from '@playwright/test';
import { container } from './stores';

const docker = (args: string[]): string => execFileSync('docker', args, { encoding: 'utf8', timeout: 60_000 }).trim();

/**
 * Stop and start one service of the stack, to see what the UI does when something it depends on is down. Always
 * `restore()` in `afterEach`: a spec that leaves a service stopped breaks every spec after it.
 */
export class Faults {
  private stopped = new Set<string>();
  private paused = new Set<string>();

  /** Freeze a service's processes: its port stays open and nothing answers, as with a sink that hangs. */
  pause(service: string): void {
    docker(['pause', container(service)]);
    this.paused.add(service);
  }

  unpause(service: string): void {
    docker(['unpause', container(service)]);
    this.paused.delete(service);
  }

  stop(service: string): void {
    docker(['stop', container(service)]);
    this.stopped.add(service);
  }

  /** Start it again and wait until it answers its health check (or, with none, is running). */
  async start(service: string): Promise<void> {
    docker(['start', container(service)]);
    this.stopped.delete(service);
    await expect
      .poll(
        () => {
          const state = docker(['inspect', '--format', '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}', container(service)]);
          return state === 'healthy' || state === 'running';
        },
        { timeout: 120_000, message: `${service} is back` },
      )
      .toBe(true);
  }

  async restore(): Promise<void> {
    for (const service of [...this.paused]) this.unpause(service);
    for (const service of [...this.stopped]) await this.start(service);
  }
}
