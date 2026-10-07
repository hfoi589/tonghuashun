import { ApiError, type IntradayMetricSeries, type MainFundFlowPeriod, type SymbolLookup } from './api'

export const marketAuthExpiredEvent = 'ths-market-auth-expired'

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

export type MarketPhase = 'CLOSED' | 'PREOPEN_QUOTE' | 'CALL_AUCTION' | 'AUCTION_LOCKED' | 'CONTINUOUS' | 'BREAK'
export type MarketQuoteSource = 'THS_AUCTION' | 'THS_DIRECT_QUOTE' | 'TENCENT_PUBLIC' | 'SINA_PUBLIC' | null

export type MarketIntradayMetricKey = 'large_order_net' | 'large_order_amount' | 'retail_count' | 'macd_dif' | 'macd_dea' | 'macdfs'

export interface MarketSnapshot {
  symbol: string
  name: string | null
  market: string
  sequence: number
  source_time: string | null
  collected_at: string
  source: MarketQuoteSource
  market_phase?: MarketPhase
  market_phase_source?: string | null
  price_precision: number
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

export interface FundFlowHistoryPoint {
  time: string
  unit: string | null
  main_net_inflow: string | null
  main_visible_inflow: string | null
  main_hidden_inflow: string | null
  retail_inflow: string | null
}

export interface FundFlowHistoryPeriod {
  points: FundFlowHistoryPoint[]
}

export interface FundFlowHistoryResponse {
  symbol: string
  name: string | null
  trade_date: string | null
  available_dates: string[]
  latest_sync_at: string | null
  last_error: string | null
  periods: {
    today: FundFlowHistoryPeriod
    three_day: FundFlowHistoryPeriod
    five_day: FundFlowHistoryPeriod
  }
}

export interface FundFlowDailyPoint {
  trade_date: string
  time: string
  unit: string | null
  main_net_inflow: string | null
  main_visible_inflow: string | null
  main_hidden_inflow: string | null
  retail_inflow: string | null
  periods?: {
    today: MainFundFlowPeriod | null
    three_day: MainFundFlowPeriod | null
    five_day: MainFundFlowPeriod | null
  } | null
}

export interface FundFlowDailyResponse {
  symbol: string
  name: string | null
  limit: number
  points: FundFlowDailyPoint[]
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
  source: 'THS_PUBLIC' | 'TENCENT_PUBLIC' | 'SINA_PUBLIC' | null
  cached: boolean
  stale: boolean
  source_errors: Record<string, string | null>
}

export interface MonitoringState {
  symbol: string
  enabled: boolean
  latest_trade_date?: string | null
  latest_time?: string | null
  last_sync_at?: string | null
  last_error?: string | null
}

export interface PortfolioMonitoringItem extends MonitoringState {
  name: string
  latest_price: string | null
  latest_signal: { side: 'buy' | 'sell', rule_title: string } | null
}

export interface PremiumSnapshot {
  as_of: string | null
  valid_count: number
  errors: Record<string, string>
  rows: Array<{
    code: string
    name: string
    current_price: number | null
    estimated_price: number | null
    premium_rate: number | null
    stale?: boolean
    error?: string | null
  }>
}

export interface ReplayRule {
  id: string
  title: string
  enabled: boolean
  side: 'buy' | 'sell'
  joiner: 'and' | 'or'
  conditions: Array<Record<string, unknown>>
}

export interface ResearchPoint {
  time: string
  price: string | null
  average_price?: string | null
  volume?: string | null
  large_order_net?: string | null
  large_order_amount?: string | null
  retail_count?: string | null
  macdfs?: string | null
  diff: number | null
  dea: number | null
  macd: number | null
}

export interface ResearchSeries {
  symbol: string
  name: string | null
  trade_date: string
  source: string | null
  available_dates: string[]
  macd_settings: { short: number, long: number, signal: number, marker_threshold: number }
  points: ResearchPoint[]
}

export interface ReplayMarker {
  index: number
  time: string
  price: number
  side: 'buy' | 'sell'
  rule_id: string
  rule_title: string
}

export interface ReplayEvaluation {
  symbol: string
  trade_date: string
  markers: ReplayMarker[]
  performance: {
    buy_count: number
    sell_count: number
    buy_cost: number
    sell_proceeds: number
    profit: number
    formula_return_pct: number | null
    forced_exit: { time: string, price: number } | null
  }
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
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new Event(marketAuthExpiredEvent))
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
  snapshot: (symbol: string, signal?: AbortSignal) => request<MarketSnapshot>(`/api/v1/market/symbols/${encodeURIComponent(symbol)}/snapshot`, { signal }),
  series: (symbol: string, period: string, signal?: AbortSignal, retry = false) => request<MarketSeriesPage>(
    `/api/v1/market/symbols/${encodeURIComponent(symbol)}/series?period=${encodeURIComponent(period)}&limit=240${retry ? "&retry=1" : ""}`,
    { signal },
  ),
  fundFlowHistory: (symbol: string, tradeDate?: string, signal?: AbortSignal) => request<FundFlowHistoryResponse>(
    `/api/v1/market/symbols/${encodeURIComponent(symbol)}/fund-flow/history${tradeDate ? `?trade_date=${encodeURIComponent(tradeDate)}` : ''}`,
    { signal },
  ),
  fundFlowDaily: (symbol: string, limit = 30, signal?: AbortSignal) => request<FundFlowDailyResponse>(
    `/api/v1/market/symbols/${encodeURIComponent(symbol)}/fund-flow/daily?limit=${limit}`,
    { signal },
  ),
  monitoring: () => request<MonitoringState[]>('/api/v1/monitoring'),
  monitoringPortfolio: () => request<{ items: PortfolioMonitoringItem[] }>('/api/v1/monitoring/portfolio'),
  premiumMonitoring: () => request<PremiumSnapshot>('/api/v1/monitoring/premium'),
  setMonitoring: (symbol: string, enabled: boolean) => request<MonitoringState>(
    `/api/v1/monitoring/symbols/${encodeURIComponent(symbol)}`,
    jsonInit('PATCH', { enabled }, true),
  ),
  backfillMonitoring: (symbol: string) => request<{ symbol: string, changed_minutes: string[] }>(
    `/api/v1/monitoring/symbols/${encodeURIComponent(symbol)}/backfill`,
    { method: 'POST', headers: { 'X-CSRF-Token': readMarketCsrfToken() } },
  ),
  replayRules: () => request<ReplayRule[]>('/api/v1/replay/rules/enabled'),
  replaySeries: (symbol: string, tradeDate?: string) => request<ResearchSeries>(
    `/api/v1/replay/${encodeURIComponent(symbol)}${tradeDate ? `?trade_date=${encodeURIComponent(tradeDate)}` : ''}`,
  ),
  evaluateReplay: (symbol: string, tradeDate: string, ruleIds: string[]) => request<ReplayEvaluation>(
    `/api/v1/replay/${encodeURIComponent(symbol)}/evaluate`,
    jsonInit('POST', { trade_date: tradeDate, rule_ids: ruleIds }, true),
  ),
}

export function readMarketCsrfToken(cookie = document.cookie): string {
  return cookie.split('; ').find((item) => item.startsWith('ths_market_csrf='))?.slice('ths_market_csrf='.length) ?? ''
}

export function marketStreamUrl(): string | null {
  if (!window.location.host) return null
  return `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/v1/market/stream`
}
