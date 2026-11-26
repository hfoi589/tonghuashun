export const captureKinds = ['LARGE_ORDER_NET', 'LARGE_ORDER_AMOUNT', 'RETAIL_COUNT'] as const
export type CaptureKind = (typeof captureKinds)[number]
export type CaptureStatus = 'PENDING' | 'READY' | 'SKIPPED' | 'EXPIRED'
export type JobStatus = 'QUEUED' | 'RUNNING' | 'WAITING_ADMIN' | 'COMPLETED' | 'MARKET_SNAPSHOT' | 'PARTIAL' | 'FAILED' | 'EXPIRED'

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
  market_snapshot?: {
    symbol: string
    name: string | null
    market: string
    source: string | null
    source_time: string | null
    collected_at: string
    stale: boolean
    age_seconds: number
    market_phase?: string | null
    quote: Record<string, string | null>
    main_fund_flow?: Partial<MainFundFlowValues>
    stored_trade_dates?: Record<string, string | null>
  } | null
  long_capture: LongCapture
}

export interface TaskFundFlowHistory {
  symbol: string
  trade_date: string
  period: 'today'
  points: Array<{
    time: string
    unit: string | null
    main_net_inflow: string | null
    main_visible_inflow: string | null
    main_hidden_inflow: string | null
    retail_inflow: string | null
  }>
}

export interface TaskFundFlowDailyPoint {
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

export interface TaskFundFlowDaily {
  symbol: string
  name: string | null
  limit: number
  baseline_trade_date: string | null
  points: TaskFundFlowDailyPoint[]
}

export type EtfFlowPeriod = '20d' | '60d' | '120d' | '250d'
export type EtfFlowTone = 'up' | 'down' | 'flat'

export interface EtfFlowSummaryMetric {
  key: string
  label: string
  value_text: string
  tone: EtfFlowTone
}

export interface EtfFlowLatestBadge {
  date: string
  full_date: string | null
  value_text: string
  text: string
  tone: EtfFlowTone
}

export interface EtfFlowChartPoint {
  date: string
  short_date: string
  cumulative_value: number | null
  cumulative_text: string | null
  daily_value: number | null
  daily_text: string | null
  close_price: number | null
  price_change: number | null
}

export interface EtfHoldingItem {
  symbol: string
  market: string
  name: string
  net_inflow_raw: number | null
  net_inflow_text: string
  tone: EtfFlowTone
  streak_days: number | null
  hold_rate: number | null
  hold_rate_text: string
}

export type EtfUpdateStatus = 'settled' | 'pending_morning_update' | 'stale_fallback' | 'partial' | 'empty'

export interface EtfSourceErrors {
  summary: string | null
  line: string | null
  holdings: string | null
  daily_kline: string | null
}

export interface EtfFundFlowResponse {
  symbol: string
  name: string | null
  market: string
  period: EtfFlowPeriod
  available: boolean
  data_date?: string | null
  expected_trade_date?: string | null
  stale?: boolean
  degraded?: boolean
  update_status?: EtfUpdateStatus
  source_errors?: EtfSourceErrors
  summary: {
    latest_badge: EtfFlowLatestBadge | null
    metrics: EtfFlowSummaryMetric[]
  }
  chart: {
    cumulative_unit: '万' | '亿'
    daily_unit: '万' | '亿'
    points: EtfFlowChartPoint[]
  }
  holdings: {
    total_count: number
    items: EtfHoldingItem[]
  }
}

export interface RequestLog { id:number; timestamp:string; ip:string|null; user_id:number|null; user_name:string|null; device_type:string|null; user_agent:string|null; path:string; action:string; symbol:string|null; stock_name:string|null; status_code:number; task_status:string|null; error_code:string|null; duration_ms:number; public_id:string|null }

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

export interface AccountSessionStatus {
  role: DeviceRole
  state: string
  updated_at: string | null
  error_code: string | null
  expires_at: string | null
}

export type DeviceRole = 'core_metrics' | 'main_fund_flow'
export type DeviceLifecycleState =
  | 'UNCONFIGURED'
  | 'UNKNOWN'
  | 'STOPPED'
  | 'STARTING'
  | 'RUNNING'
  | 'STOPPING'
  | 'ERROR'
export type DeviceLifecycleAction = 'start_and_launch_app' | 'shutdown'

export interface AdminDeviceHealth {
  role: DeviceRole
  label: string
  adb: string
  app: string
  frida: string
  lifecycle: {
    state: DeviceLifecycleState
    operation_id: string | null
    error_code: string | null
    updated_at: string | null
  }
}

export interface MarketAdminUser {
  id: number
  username: string
  enabled: boolean
  must_change_password: boolean
  role: 'admin' | 'user'
  created_at: string
}

export interface AdminMonitoringStatus {
  symbol: string
  latest_trade_date: string | null
  latest_time: string | null
  last_sync_at: string | null
  last_error: string | null
}

export interface AdminMarketMonitoringItem {
  symbol: string
  stock_name: string
  monitoring_date: string | null
  monitoring_users: string[]
}

export interface AdminMacdSettings {
  short: number
  long: number
  signal: number
  marker_threshold: number
}

export interface AdminVibeSettings {
  ai: {
    configured: boolean
    provider: string | null
    baseURL: string | null
    model: string | null
    key_present: boolean
    subscription: boolean
  }
  agents: Array<{ provider: string; name?: string; status?: string; available?: boolean; detail?: string; version?: string }>
  sec_contact: { source: 'env' | 'saved' | 'none'; masked: string | null; valid: boolean } | null
  update: {
    current: string
    install: 'portable' | 'source'
    enabled: boolean
    env_disabled: boolean
    skip_version: string | null
    checked_at: string | null
    latest: { version: string; url: string; published_at: string | null } | null
    error: string | null
    update_available: boolean
  }
}

export interface AdminVibeLlmConfig {
  provider: string
  baseURL: string
  model: string
  apiKey: string
}

export interface AdminPushConfig {
  enabled: boolean
  premium_push_enabled: boolean
  bark_groups: Array<{ id: string, name: string, base_url: string, device_key: string, device_key_configured?: boolean }>
  sc3_bot: { enabled: boolean, base_url: string, token: string, token_configured?: boolean, chat_id: string, parse_mode?: string, silent?: boolean }
  wecom: { enabled: boolean, api_base_url: string, news_base_url?: string, corp_id: string, corp_secret: string, corp_secret_configured?: boolean, agent_id: number, to_user: string, to_party?: string, to_tag?: string }
  rules: Array<Record<string, unknown>>
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

export interface AdminMailConfig {
  host: string
  port: number
  ssl: boolean
  user: string
  password_configured: boolean
  sender: string
  registration_enabled: boolean
  ready: boolean
}

export interface AdminMailConfigInput {
  host: string
  port: number
  ssl: boolean
  user: string
  password: string
  sender: string
  registration_enabled: boolean
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
    `/api/v1/symbols?query=${encodeURIComponent(query)}&limit=10`,
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
  fundFlowHistory: (publicId: string) => request<TaskFundFlowHistory>(`/api/v1/jobs/${encodeURIComponent(publicId)}/fund-flow-history`, { cache: 'no-store' }),
  fundFlowDaily: (publicId: string, limit = 30) => request<TaskFundFlowDaily>(`/api/v1/jobs/${encodeURIComponent(publicId)}/fund-flow-daily?limit=${limit}`, { cache: 'no-store' }),
  etfFlow: (publicId: string, period: EtfFlowPeriod = '60d', signal?: AbortSignal) => request<EtfFundFlowResponse>(
    `/api/v1/jobs/${encodeURIComponent(publicId)}/etf-flow?period=${encodeURIComponent(period)}`,
    { cache: 'no-store', signal },
  ),
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
  logs: (params = '') => request<{ items: RequestLog[]; total: number; limit: number; offset: number }>(`/api/admin/logs${params}`),
  runner: () => request<RunnerHealth>('/api/admin/runner'),
  lock: () => request<LockState>('/api/admin/lock'),
  queue: () => request<QueueState>('/api/admin/queue'),
  devices: () => request<{ devices: AdminDeviceHealth[] }>('/api/admin/devices'),
  deviceAction: (role: DeviceRole, action: DeviceLifecycleAction, csrfToken: string) => request<AdminDeviceHealth['lifecycle']>(`/api/admin/devices/${role}/actions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ action }),
  }),
  accountSessions: () => request<{ sessions: AccountSessionStatus[] }>('/api/admin/account-sessions'),
  refreshAccountSession: (role: AccountSessionStatus['role'], csrfToken: string) => request<AccountSessionStatus>(`/api/admin/account-sessions/${role}/refresh`, {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
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
  monitoringStatus: () => request<AdminMonitoringStatus[]>('/api/admin/monitoring'),
  marketMonitoringList: () => request<AdminMarketMonitoringItem[]>('/api/admin/market-monitoring-list'),
  refreshMonitoring: (csrfToken: string) => request<{ symbols: Record<string, string[]> }>('/api/admin/monitoring/refresh', {
    method: 'POST', headers: { 'X-CSRF-Token': csrfToken },
  }),
  macdSettings: () => request<AdminMacdSettings>('/api/admin/research/macd'),
  saveMacdSettings: (settings: AdminMacdSettings, csrfToken: string) => request<AdminMacdSettings>('/api/admin/research/macd', {
    method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(settings),
  }),
  vibeSettings: () => request<AdminVibeSettings>('/api/admin/vibe/settings'),
  testVibeSettings: (llm: AdminVibeLlmConfig, csrfToken: string) => request<{ ok: true; duration_ms?: number }>('/api/admin/vibe/settings/test', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ llm }),
  }),
  saveVibeSettings: (settings: { llm: AdminVibeLlmConfig; sec_contact?: string | null; codebuddy_path?: string | null }, csrfToken: string) => request<{ ai: AdminVibeSettings['ai']; sec_contact: AdminVibeSettings['sec_contact'] }>('/api/admin/vibe/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(settings),
  }),
  clearVibeSettings: (csrfToken: string) => request<{ ai: AdminVibeSettings['ai'] }>('/api/admin/vibe/settings', {
    method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken },
  }),
  saveVibeSecContact: (contact: string | null, csrfToken: string) => request<NonNullable<AdminVibeSettings['sec_contact']>>('/api/admin/vibe/settings/sec-contact', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ contact }),
  }),
  saveVibeCodeBuddyPath: (path: string | null, csrfToken: string) => request<Record<string, unknown>>('/api/admin/vibe/settings/codebuddy-path', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ path }),
  }),
  updateVibeSettings: (action: 'disable' | 'enable' | 'skip' | 'check', version: string | undefined, csrfToken: string) => request<AdminVibeSettings['update']>('/api/admin/vibe/settings/update', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(version === undefined ? { action } : { action, version }),
  }),
  mailConfig: () => request<AdminMailConfig>('/api/admin/mail/config'),
  saveMailConfig: (config: AdminMailConfigInput, csrfToken: string) => request<AdminMailConfig>('/api/admin/mail/config', {
    method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(config),
  }),
  testMail: (to: string, csrfToken: string) => request<{ sent: boolean }>('/api/admin/mail/test', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ to }),
  }),
  pushConfig: () => request<AdminPushConfig>('/api/admin/push/config'),
  savePushConfig: (config: AdminPushConfig, csrfToken: string) => request<AdminPushConfig>('/api/admin/push/config', {
    method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(config),
  }),
  testPush: (csrfToken: string) => request<Record<string, unknown>>('/api/admin/push/test', {
    method: 'POST', headers: { 'X-CSRF-Token': csrfToken },
  }),
  premium: () => request<import('./market-api').PremiumSnapshot>('/api/admin/premium'),
  refreshPremium: (csrfToken: string) => request<import('./market-api').PremiumSnapshot>('/api/admin/premium/refresh', {
    method: 'POST', headers: { 'X-CSRF-Token': csrfToken },
  }),
  pushPremium: (csrfToken: string) => request<Record<string, unknown>>('/api/admin/premium/push', {
    method: 'POST', headers: { 'X-CSRF-Token': csrfToken },
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
