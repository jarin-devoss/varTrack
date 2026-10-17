export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly endpoint: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * The server answered, but does not implement the endpoint (404/405/501).
 * Screens render this as "not available on this server" instead of a failure.
 */
export class NotAvailableError extends ApiError {
  constructor(endpoint: string, status: number) {
    super(`${endpoint} is not available on this server`, status, endpoint);
    this.name = 'NotAvailableError';
  }
}

/** The orchestrator answered 401: single sign-on is on and the session is missing or over. */
export class UnauthenticatedError extends ApiError {
  constructor(endpoint: string) {
    super('sign in required', 401, endpoint);
    this.name = 'UnauthenticatedError';
  }
}

let onUnauthenticated: (() => void) | null = null;

/** Called once per 401, so the app can send the user to the sign-in page. */
export function setUnauthenticatedHandler(handler: (() => void) | null): void {
  onUnauthenticated = handler;
}

export interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  token?: string;
  tenant?: string;
  timeoutMs?: number;
  /** Return the parsed body for these non-2xx statuses instead of throwing. */
  acceptStatus?: number[];
}

export interface JsonResponse<T> {
  status: number;
  body: T;
}

export async function requestJson<T>(url: string, opts: RequestOptions = {}): Promise<JsonResponse<T>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 15000);
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  if (opts.tenant) headers['X-Tenant-ID'] = opts.tenant;

  let res: Response;
  try {
    res = await fetch(url, {
      method: opts.method ?? 'GET',
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: controller.signal,
    });
  } catch (err) {
    const reason = controller.signal.aborted ? 'request timed out' : 'service unreachable';
    throw new ApiError(`${reason} (${url})`, 0, url);
  } finally {
    clearTimeout(timer);
  }

  const text = await res.text();
  let body: unknown = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // Plain-text bodies (e.g. "OK" from a liveness probe) are passed through.
  }

  if (res.ok || opts.acceptStatus?.includes(res.status)) {
    return { status: res.status, body: body as T };
  }
  if (res.status === 404 || res.status === 405 || res.status === 501) {
    throw new NotAvailableError(url, res.status);
  }
  if (res.status === 401 && errorMessage(body) === 'sign in required') {
    // The orchestrator's single sign-on; a gateway 401 is about the bearer token instead.
    onUnauthenticated?.();
    throw new UnauthenticatedError(url);
  }
  const detail = errorMessage(body);
  if (!detail && res.status >= 500) {
    // A proxy in front of a stopped service answers 5xx with no body.
    throw new ApiError(`no usable response from ${url} (HTTP ${res.status}) — is the service running?`, res.status, url);
  }
  throw new ApiError(detail || `HTTP ${res.status}`, res.status, url);
}

function errorMessage(body: unknown): string {
  if (typeof body === 'string') return body.trim();
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>;
    if (typeof b.detail === 'string') return b.detail;
    if (Array.isArray(b.detail)) return b.detail.map(d => (d as { msg?: string }).msg ?? String(d)).join('; ');
    if (typeof b.message === 'string') return b.message;
  }
  return '';
}

export function isNotAvailable(err: unknown): err is NotAvailableError {
  return err instanceof NotAvailableError;
}
