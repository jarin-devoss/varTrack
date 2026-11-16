import type { FullConfig } from '@playwright/test';
import { signInEveryone } from '../global-setup';

/**
 * The tour is about the demo as it is seeded, so unlike the browser suite it does not build a world (e2e/fixtures/world.ts
 * rewrites the demo's rules and branches). It only signs in as the four people.
 */
export default async function globalSetup(config: FullConfig): Promise<void> {
  await signInEveryone(config.projects[0].use.baseURL ?? 'http://localhost:8080');
}
