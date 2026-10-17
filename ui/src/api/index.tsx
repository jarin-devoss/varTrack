import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type {
  AuthSession,
  BundleView,
  CliSyncRequest,
  EventQuery,
  KeyEntry,
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
  listRules(): Promise<RuleView[]>;
  getRule(name: string): Promise<RuleView>;
  listKeys(rule: string, env: string): Promise<KeyEntry[]>;
  listEvents(query?: EventQuery): Promise<VtEvent[]>;
  listTasks(query?: TaskQuery): Promise<Task[]>;
  getTask(taskId: string): Promise<Task>;
  /** Re-read the rule's file from Git and write it to the chosen environments. */
  sync(req: SyncRequest): Promise<{ taskIds: string[] }>;
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
  const { gatewayUrl, orchestratorUrl, watcherUrl, token, tenant } = prefs.connection;
  const api = useMemo(
    () => createLiveApi({ gatewayUrl, orchestratorUrl, watcherUrl, token, tenant }),
    [gatewayUrl, orchestratorUrl, watcherUrl, token, tenant],
  );
  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>;
}

export function useApi(): VarTrackApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error('useApi must be used inside <ApiProvider>');
  return api;
}
