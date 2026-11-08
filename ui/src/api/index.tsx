import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type {
  AuthSession,
  BundleView,
  CliSyncRequest,
  EventQuery,
  KeyEntry,
  RuleList,
  RuleView,
  ServiceHealth,
  SyncRequest,
  Task,
  TaskQuery,
  ValidateRequest,
  ValidateResult,
  VtEvent,
} from './types';
import { createLiveApi } from './live';
import { usePrefs } from '../lib/prefs';

export interface VarTrackApi {
  /** Every rule the viewer may see, of every tenant, and what a connection would show more. */
  listRules(): Promise<RuleList>;
  /** `tenant` is needed only when the rule name exists in several tenants. */
  /** *fresh* reads the sinks now instead of answering from the last read (what Refresh asks for). */
  getRule(name: string, tenant?: string, options?: { fresh?: boolean }): Promise<RuleView>;
  listKeys(rule: string, env: string, tenant?: string): Promise<KeyEntry[]>;
  listEvents(query?: EventQuery): Promise<VtEvent[]>;
  listTasks(query?: TaskQuery): Promise<Task[]>;
  getTask(taskId: string): Promise<Task>;
  /** Re-read the rule's file from Git and write it to the chosen environments. */
  sync(req: SyncRequest): Promise<{ taskIds: string[] }>;
  /** Forget an environment no branch feeds any more (an administrator's action). */
  forgetEnvironment(rule: string, env: string, tenant?: string): Promise<void>;
  validate(req: ValidateRequest): Promise<ValidateResult>;
  /** Push raw file content through the pipeline, as `vt sync` does. */
  cliSync(req: CliSyncRequest): Promise<{ taskId: string }>;
  getHealth(): Promise<ServiceHealth[]>;
  getBundle(): Promise<BundleView>;
  /** Is sign-in on, and who is signed in. */
  getSession(): Promise<AuthSession>;
  /** Where the browser goes to sign in with (or connect) a Git platform. */
  loginUrl(platform: string, returnTo: string): string;
  logout(): Promise<void>;
}

const ApiContext = createContext<VarTrackApi | null>(null);

export function ApiProvider({ children }: { children: ReactNode }) {
  const [prefs] = usePrefs();
  const { gatewayUrl, orchestratorUrl, watcherUrl, token } = prefs.connection;
  const api = useMemo(
    () => createLiveApi({ gatewayUrl, orchestratorUrl, watcherUrl, token }),
    [gatewayUrl, orchestratorUrl, watcherUrl, token],
  );
  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>;
}

export function useApi(): VarTrackApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error('useApi must be used inside <ApiProvider>');
  return api;
}
