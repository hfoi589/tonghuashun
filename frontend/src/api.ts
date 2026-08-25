export const captureKinds = ['LARGE_ORDER_NET', 'LARGE_ORDER_AMOUNT', 'RETAIL_COUNT'] as const
export type CaptureKind = (typeof captureKinds)[number]
export type CaptureStatus = 'PENDING' | 'READY' | 'SKIPPED' | 'EXPIRED'
export type JobStatus = 'QUEUED' | 'RUNNING' | 'WAITING_ADMIN' | 'COMPLETED' | 'PARTIAL' | 'FAILED' | 'EXPIRED'

export interface Capture {
  kind: CaptureKind
  status: CaptureStatus
  url: string | null
  expires_at: string | null
}

export interface MainFundFlowPeriod {
  unit: string | null
  main_net_inflow: string | null
  main_visible_inflow: string | null
  main_hidden_inflow: string | null
  retail_inflow: string | null
}

export interface MainFundFlowValues {
  today: MainFundFlowPeriod
  three_day: MainFundFlowPeriod
  five_day: MainFundFlowPeriod
}

export interface IntradayPoint {
  time: string
  value: string | null
}

export interface IntradayMetricSeries {
  unit: string | null
  points: IntradayPoint[]
}

export interface IntradaySeriesValues {
  large_order_net: IntradayMetricSeries
  large_order_amount: IntradayMetricSeries
  retail_count: IntradayMetricSeries
}

export interface JobValues {
  stock_name: string | null
  current_price: string | null
  change_percent: string | null
  turnover_rate: string | null
  large_order_net: string | null
  large_order_amount: string | null
  retail_count: string | null
  macdfs: string | null
  intraday_series?: IntradaySeriesValues
  main_fund_flow: MainFundFlowValues
}

export type ValueSource = 'INTERFACE' | 'OCR'

export interface JobValueSources {
  stock_name: ValueSource | null
  current_price: ValueSource | null
  change_percent: ValueSource | null
  turnover_rate: ValueSource | null
  large_order_net: ValueSource | null
  large_order_amount: ValueSource | null
  retail_count: ValueSource | null
  macdfs: ValueSource | null
  intraday_series?: {
    large_order_net: ValueSource | null
    large_order_amount: ValueSource | null
    retail_count: ValueSource | null
  }
  main_fund_flow: {
    today: MainFundFlowPeriodSources
    three_day: MainFundFlowPeriodSources
    five_day: MainFundFlowPeriodSources
  }
}

export interface MainFundFlowPeriodSources {
  main_net_inflow: ValueSource | null
  main_visible_inflow: ValueSource | null
  main_hidden_inflow: ValueSource | null
  retail_inflow: ValueSource | null
}

export interface LongCapture {
  status: CaptureStatus
  url: string | null
  expires_at: string | null
}

export interface Job {
  public_id: string
  symbol: string
  include_long_capture: boolean
  status: JobStatus
  error_code: string | null
  source_errors?: {
    core_metrics: string | null
    main_fund_flow: string | null
  }
  queue_position?: number | null
  created_at: string
  collected_at: string | null
  captures: Capture[]
  values: JobValues
  value_sources?: JobValueSources
  long_capture: LongCapture
}

export interface RunnerHealth {
  state: string
  last_heartbeat: string | null
  queue_paused: boolean
}

export interface LockState {
  locked: boolean
}

export interface QueueState {
  paused: boolean
}

export interface MarketAdminUser {
  id: number
  username: string
  enabled: boolean
  must_change_password: boolean
  created_at: string
}

export interface SymbolLookup {
  symbol: string
  name: string
  market: string
}

export interface SymbolSuggestion extends SymbolLookup {
  market_label: string | null
}

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
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
      // HTTP status is still a useful, safe failure signal.
    }
    throw new ApiError(response.status, message)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}

export const api = {
  searchSymbols: (query: string, signal?: AbortSignal) => request<SymbolSuggestion[]>(
    `/api/v1/symbols?query=${encodeURIComponent(query)}&limit=8`,
    { signal },
  ),
  lookupSymbol: (symbol: string, signal?: AbortSignal) => request<SymbolLookup>(
    `/api/v1/symbols/${encodeURIComponent(symbol)}`,
    { signal },
  ),
  submitJob: (symbol: string, includeLongCapture = true) => request<Job>('/api/v1/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol, include_long_capture: includeLongCapture }),
  }),
  getJob: (publicId: string) => request<Job>(`/api/v1/jobs/${encodeURIComponent(publicId)}`),
  retryJob: (publicId: string) => request<Job>(`/api/v1/jobs/${encodeURIComponent(publicId)}/retry`, {
    method: 'POST',
  }),
  login: (password: string) => request<void>('/api/admin/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  }),
  adminSession: () => request<void>('/api/admin/session'),
  changePassword: (currentPassword: string, newPassword: string, confirmation: string, csrfToken: string) => request<void>('/api/admin/password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
      new_password_confirmation: confirmation,
    }),
  }),
  logout: (csrfToken: string) => request<void>('/api/admin/session/logout', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  runner: () => request<RunnerHealth>('/api/admin/runner'),
  lock: () => request<LockState>('/api/admin/lock'),
  queue: () => request<QueueState>('/api/admin/queue'),
  pauseQueue: (csrfToken: string) => request<QueueState>('/api/admin/queue/pause', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  resumeQueue: (csrfToken: string) => request<QueueState>('/api/admin/queue/resume', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  resumeWaitingJob: (publicId: string, csrfToken: string) => request<Job>(`/api/admin/jobs/${encodeURIComponent(publicId)}/resume`, {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  retryFailedJob: (publicId: string, csrfToken: string) => request<Job>(`/api/admin/jobs/${encodeURIComponent(publicId)}/retry`, {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  acquireLock: (csrfToken: string) => request<LockState>('/api/admin/lock/acquire', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  releaseLock: (csrfToken: string) => request<LockState>('/api/admin/lock/release', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  marketUsers: () => request<MarketAdminUser[]>('/api/admin/users'),
  createMarketUser: (username: string, temporaryPassword: string, csrfToken: string) => request<MarketAdminUser>('/api/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ username, temporary_password: temporaryPassword }),
  }),
  updateMarketUser: (userId: number, update: { enabled?: boolean, temporary_password?: string }, csrfToken: string) => request<MarketAdminUser>(`/api/admin/users/${userId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify(update),
  }),
}

export type JobStreamState = 'CONNECTED' | 'RECONNECTING' | 'UNAVAILABLE'

export function subscribeToJob(publicId: string, onChange: () => void, onStateChange?: (state: JobStreamState) => void): () => void {
  if (typeof EventSource === 'undefined') return () => undefined
  const stream = new EventSource(`/api/v1/jobs/${encodeURIComponent(publicId)}/events`)
  stream.addEventListener('status', onChange)
  stream.onopen = () => onStateChange?.('CONNECTED')
  stream.onerror = () => onStateChange?.('RECONNECTING')
  return () => stream.close()
}

export function readCsrfToken(cookie = document.cookie): string {
  return cookie.split('; ').find((item) => item.startsWith('ths_csrf='))?.slice('ths_csrf='.length) ?? ''
}
