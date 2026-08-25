import { ApiError, type IntradayMetricSeries, type SymbolLookup } from './api'

export interface MarketUser {
  id: number
  username: string
  enabled: boolean
  must_change_password: boolean
  created_at: string
}

export interface WatchlistItem {
  symbol: string
  name: string
  market: string
}

export interface WatchlistGroup {
  id: number
  name: string
  sort_order: number
  is_primary?: boolean
  items: WatchlistItem[]
}

export interface TimesharePoint {
  time: string
  price: string | null
  average_price: string | null
  volume: string | null
}

export interface MarketCapability {
  available: boolean
  reason?: string | null
  adjustment?: string
}

export type MarketIntradayMetricKey = 'large_order_net' | 'large_order_amount' | 'retail_count' | 'macd_dif' | 'macd_dea' | 'macdfs'

export interface MarketSnapshot {
  symbol: string
  name: string | null
  market: string
  sequence: number
  source_time: string | null
  collected_at: string
  stale: boolean
  age_seconds: number
  quote: Record<string, string | null>
  timeshare: TimesharePoint[]
  intraday_series: Partial<Record<MarketIntradayMetricKey, IntradayMetricSeries>>
  order_book: Array<{ side: string, level: number, price: string | null, volume: string | null }>
  trades: Array<{ time: string, price: string | null, volume: string | null, side: string | null }>
  main_fund_flow: Record<string, Record<string, string | null>>
  capabilities: Record<string, MarketCapability>
  source_errors: Record<string, string | null>
}

export interface KlineBar {
  time: string
  open: string | null
  high: string | null
  low: string | null
  close: string | null
  volume: string | null
  amount: string | null
}

export interface MarketSeriesPage {
  symbol: string
  period: string
  bars: KlineBar[]
  indicators: Record<string, Array<string | null>>
  next_cursor: string | null
  source_error: string | null
  adjustment: 'qfq' | null
  source: 'THS_PUBLIC' | 'THS_APP' | null
  cached: boolean
  stale: boolean
  source_errors: Record<string, string | null>
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: 'include', ...init })
  if (!response.ok) {
    let message = '请求未完成'
    try {
      const body = await response.json() as { detail?: string }
      message = body.detail ?? message
    } catch {
      // The status code remains available to callers.
    }
    throw new ApiError(response.status, message)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}

function jsonInit(method: string, body: unknown, csrf = false): RequestInit {
  return {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRF-Token': readMarketCsrfToken() } : {}),
    },
    body: JSON.stringify(body),
  }
}

export const marketApi = {
  session: () => request<MarketUser>('/api/v1/session'),
  login: (username: string, password: string) => request<MarketUser>(
    '/api/v1/session',
    jsonInit('POST', { username, password }),
  ),
  logout: () => request<void>('/api/v1/session', {
    method: 'DELETE',
    headers: { 'X-CSRF-Token': readMarketCsrfToken() },
  }),
  changePassword: (currentPassword: string, newPassword: string, confirmation: string) => request<void>(
    '/api/v1/session/password',
    jsonInit('POST', {
      current_password: currentPassword,
      new_password: newPassword,
      new_password_confirmation: confirmation,
    }, true),
  ),
  watchlists: () => request<{ groups: WatchlistGroup[] }>('/api/v1/watchlists'),
  createGroup: (name: string) => request<WatchlistGroup>(
    '/api/v1/watchlists/groups',
    jsonInit('POST', { name }, true),
  ),
  addSymbol: (groupId: number, symbol: string) => request<WatchlistItem>(
    `/api/v1/watchlists/groups/${groupId}/symbols`,
    jsonInit('POST', { symbol }, true),
  ),
  removeSymbol: (groupId: number, symbol: string) => request<void>(
    `/api/v1/watchlists/groups/${groupId}/symbols/${encodeURIComponent(symbol)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': readMarketCsrfToken() } },
  ),
  removeSymbolEverywhere: (symbol: string) => request<void>(
    `/api/v1/watchlists/symbols/${encodeURIComponent(symbol)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': readMarketCsrfToken() } },
  ),
  lookupSymbol: (symbol: string) => request<SymbolLookup>(`/api/v1/symbols/${encodeURIComponent(symbol)}`),
  snapshot: (symbol: string) => request<MarketSnapshot>(`/api/v1/market/symbols/${encodeURIComponent(symbol)}/snapshot`),
  series: (symbol: string, period: string) => request<MarketSeriesPage>(
    `/api/v1/market/symbols/${encodeURIComponent(symbol)}/series?period=${encodeURIComponent(period)}&limit=240`,
  ),
}

export function readMarketCsrfToken(cookie = document.cookie): string {
  return cookie.split('; ').find((item) => item.startsWith('ths_market_csrf='))?.slice('ths_market_csrf='.length) ?? ''
}

export function marketStreamUrl(): string | null {
  if (!window.location.host) return null
  return `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/v1/market/stream`
}
