/**
 * Thin, typed fetch wrapper over the FastAPI layer. No caching, no retries --
 * this is a localhost, single-user app talking to a server on the same
 * machine (see claudetrade.webapi's module docstring), so the failure modes
 * a public API client worries about mostly don't apply here.
 */

import type {
  DashboardData,
  AIConfig,
  AIConfigUpdateResult,
  CredentialsResponse,
  CredentialTestResult,
  DiagnosticsResponse,
  Meta,
  PaperAccountResponse,
  PaperOpenResponse,
  Performance,
  RefreshRequest,
  RefreshResponse,
  RefreshStatus,
  RejectedResponse,
  ScanRequest,
  ScanResponse,
  SignalDetail,
  SignalFilters,
  SignalList,
  TickerDetail,
} from './types';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail ?? detail;
    } catch {
      // response body wasn't JSON; keep statusText
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

function query(params: Record<string, string | number | boolean | string[] | undefined>): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const v of value) usp.append(key, v);
    } else {
      usp.append(key, String(value));
    }
  }
  const qs = usp.toString();
  return qs ? `?${qs}` : '';
}

export const api = {
  meta: () => request<Meta>('/api/meta'),

  credentials: () => request<CredentialsResponse>('/api/system/credentials'),
  saveCredential: (name: string, value: string) =>
    request<{ name: string; configured: boolean; source: string }>(`/api/system/credentials/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({ value }) }),
  deleteCredential: (name: string) =>
    request<void>(`/api/system/credentials/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  diagnostics: () => request<DiagnosticsResponse>('/api/system/diagnostics'),
  testCredential: (source: string) =>
    request<CredentialTestResult>(`/api/system/credentials/${encodeURIComponent(source)}/test`, {
      method: 'POST',
    }),

  aiConfig: () => request<AIConfig>('/api/system/ai-config'),
  updateAIConfig: (provider: string, model: string) =>
    request<AIConfigUpdateResult>('/api/system/ai-config', {
      method: 'PUT',
      body: JSON.stringify({ provider, model }),
    }),

  refreshStatus: () => request<RefreshStatus>('/api/system/refresh/status'),
  startBackgroundRefresh: () =>
    request<{ started: boolean }>('/api/system/refresh', { method: 'POST' }),

  listSignals: (filters: SignalFilters = {}) =>
    request<SignalList>(
      `/api/signals${query({
        direction: filters.direction,
        min_score: filters.min_score,
        min_confidence: filters.min_confidence,
        strategy: filters.strategy,
        max_days_to_earnings: filters.max_days_to_earnings,
        limit: filters.limit,
        distinct: filters.distinct,
      })}`,
    ),

  getSignal: (signalId: string) =>
    request<SignalDetail>(`/api/signals/${encodeURIComponent(signalId)}`),

  rejectedCandidates: () => request<RejectedResponse>('/api/signals/rejected'),

  runScan: (body: ScanRequest = {}) =>
    request<ScanResponse>('/api/scan', { method: 'POST', body: JSON.stringify(body) }),

  runRefresh: (body: RefreshRequest = {}) =>
    request<RefreshResponse>('/api/refresh', { method: 'POST', body: JSON.stringify(body) }),

  listTickers: () => request<string[]>('/api/tickers'),

  tickerDetail: (symbol: string, lookbackDays = 180) =>
    request<TickerDetail>(
      `/api/tickers/${encodeURIComponent(symbol)}${query({ lookback_days: lookbackDays })}`,
    ),

  dashboard: () => request<DashboardData>('/api/dashboard'),

  paperAccount: () => request<PaperAccountResponse>('/api/paper/account'),

  paperPerformance: () => request<Performance>('/api/paper/performance'),

  paperOpen: (signalId: string) =>
    request<PaperOpenResponse>('/api/paper/open', {
      method: 'POST',
      body: JSON.stringify({ signal_id: signalId }),
    }),
};

export type Api = typeof api;
