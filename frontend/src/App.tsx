import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AdminPage } from './AdminPage'
import { ApiError, api, type IntradaySeriesValues, type Job, type JobStatus, type JobStreamState, type MainFundFlowPeriod, type MainFundFlowValues, type SymbolLookup, type SymbolSuggestion, type TaskFundFlowDaily, type TaskFundFlowHistory, type ValueSource, subscribeToJob } from './api'
import { IntradayMetricChart } from './IntradayMetricChart'
import { FundFlowHistoryChart } from './FundFlowHistoryChart'
import { FundFlowDailyChart } from './FundFlowDailyChart'
import type { FundFlowDailyPoint } from './market-api'
import './styles.css'

const LEGACY_HISTORY_STORAGE_KEY = 'ths_level2_job_history'
const STOCK_TABS_STORAGE_KEY = 'ths_level2_stock_tabs_v2'
const ACTIVE_TAB_STORAGE_KEY = 'ths_level2_active_stock_tab'
const MAX_STOCK_TABS = 50
const terminalStatuses = new Set<JobStatus>(['COMPLETED', 'MARKET_SNAPSHOT', 'PARTIAL', 'FAILED', 'EXPIRED'])
const loadingStatuses = new Set<JobStatus>(['QUEUED', 'RUNNING', 'WAITING_ADMIN'])

interface StoredStockTab {
  public_id: string
  symbol: string
  name: string
}

interface StockTab extends StoredStockTab {
  task: Job
}

type SymbolLookupState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'valid', result: SymbolLookup }
  | { status: 'invalid' }
  | { status: 'unavailable' }

type SymbolSuggestionState = 'idle' | 'loading' | 'ready' | 'empty' | 'unavailable'

const statusText: Record<JobStatus, string> = {
  QUEUED: '已进入队列',
  RUNNING: '正在采集',
  WAITING_ADMIN: '等待管理员处理',
  COMPLETED: '数据已就绪',
  MARKET_SNAPSHOT: '盘后快照',
  PARTIAL: '部分数据未读取',
  FAILED: '采集未完成',
  EXPIRED: '结果已过期',
}

function taskStatusText(task: Job): string {
  if (task.status === 'MARKET_SNAPSHOT' && task.market_snapshot?.market_phase === 'BREAK') {
    return '午间休市'
  }
  return statusText[task.status]
}

function readHistoryIds(): string[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(LEGACY_HISTORY_STORAGE_KEY) ?? '[]')
    if (!Array.isArray(value)) return []
    return [...new Set(value.filter((item): item is string => (
      typeof item === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(item)
    )))]
  } catch {
    return []
  }
}

function readStoredTabs(): StoredStockTab[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(STOCK_TABS_STORAGE_KEY) ?? '[]')
    if (!Array.isArray(value)) return []
    const seenSymbols = new Set<string>()
    return value.flatMap((item) => {
      const candidate = item as Record<string, unknown>
      if (
        typeof item !== 'object' || item === null
        || typeof candidate.public_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(candidate.public_id)
        || typeof candidate.symbol !== 'string' || !/^\d{6}$/.test(candidate.symbol)
        || typeof candidate.name !== 'string'
        || seenSymbols.has(candidate.symbol)
      ) return []
      seenSymbols.add(candidate.symbol)
      return [{ public_id: candidate.public_id, symbol: candidate.symbol, name: candidate.name }]
    }).slice(0, MAX_STOCK_TABS)
  } catch {
    return []
  }
}

function persistStockTabs(tabs: StockTab[]): void {
  if (tabs.length === 0) {
    window.localStorage.removeItem(STOCK_TABS_STORAGE_KEY)
    return
  }
  window.localStorage.setItem(STOCK_TABS_STORAGE_KEY, JSON.stringify(tabs.map(({ public_id, symbol, name }) => ({
    public_id,
    symbol,
    name,
  }))))
}

function persistActiveTab(publicId: string | null): void {
  if (publicId === null) window.localStorage.removeItem(ACTIVE_TAB_STORAGE_KEY)
  else window.localStorage.setItem(ACTIVE_TAB_STORAGE_KEY, publicId)
}

function tabName(task: Job, fallback?: string): string {
  return task.values.stock_name || fallback || task.symbol
}

type MarketTone = 'up' | 'down' | 'neutral'

function formatDirectionalValue(value: string): { text: string, tone: MarketTone } {
  const match = value.match(/^([+-]?)(\d[\d,]*(?:\.\d+)?)(.*)$/)
  if (!match) return { text: value, tone: 'neutral' }

  const [, sign, digits, suffix] = match
  const numericValue = Number(`${sign}${digits.replaceAll(',', '')}`)
  if (!Number.isFinite(numericValue)) return { text: value, tone: 'neutral' }
  if (numericValue > 0) return { text: `+${digits}${suffix}`, tone: 'up' }
  if (numericValue < 0) return { text: `-${digits}${suffix}`, tone: 'down' }
  return { text: `${digits}${suffix}`, tone: 'neutral' }
}

function formatSnapshotLargeOrderAmount(value: string | null | undefined): string | null {
  if (value == null) return null
  const normalized = value.trim()
  if (!/^[+-]?\d[\d,]*(?:\.\d+)?$/.test(normalized)) return normalized
  return `${normalized}万`
}

function ValueCard({ name, value, source, finished, directional = false }: {
  name: string,
  value: string | null,
  source?: ValueSource | null,
  finished: boolean,
  directional?: boolean,
}) {
  const displayValue = value
    ? directional ? formatDirectionalValue(value) : { text: value, tone: 'neutral' as const }
    : null

  return <article className="value-card">
    <p className="value-label">
      {name}
      {source === 'OCR' && <span className="ocr-source">OCR识别</span>}
    </p>
    <p className={displayValue ? `indicator-value market-${displayValue.tone}` : 'minor value-placeholder'}>
      {displayValue?.text ?? (finished ? '未识别' : '待采集')}
    </p>
  </article>
}

function QuoteSummary({ task, finished }: { task: Job, finished: boolean }) {
  const change = task.values.change_percent
    ? formatDirectionalValue(task.values.change_percent)
    : null
  const quoteName = task.values.stock_name || task.symbol
  const priceTone = change?.tone ?? 'neutral'
  return <section className="quote-summary" aria-label="行情摘要">
    <div className="quote-primary">
      <div className="quote-stock">
        <span className="quote-stock-label sr-only">股票</span>
        <strong>{quoteName}</strong>
        <small className="quote-stock-symbol">{task.symbol}</small>
      </div>
      <div className="quote-metric quote-price-stack">
        <p className="quote-metric-label sr-only">当前股价</p>
        <p className={`quote-metric-value market-${priceTone} quote-price`}>{task.values.current_price ?? '未识别'}</p>
        <p aria-label="当前涨跌幅" className={`quote-change-value market-${priceTone}`}>{change?.text ?? '未识别'}</p>
      </div>
    </div>
    <div className="quote-secondary-metrics compact-metrics">
      <ValueCard name="换手率" value={task.values.turnover_rate} source={task.value_sources?.turnover_rate} finished={finished} />
      <ValueCard name="大单净量" value={task.values.large_order_net} source={task.value_sources?.large_order_net} finished={finished} directional />
      <ValueCard name="大单金额" value={task.values.large_order_amount} source={task.value_sources?.large_order_amount} finished={finished} directional />
      <ValueCard name="散户数量" value={task.values.retail_count} source={task.value_sources?.retail_count} finished={finished} directional />
      <ValueCard name="MACDFS" value={task.values.macdfs} source={task.value_sources?.macdfs} finished={finished} directional />
    </div>
  </section>
}

function JobResultSkeleton() {
  return <div className="job-result-skeleton" data-testid="job-result-skeleton">
    <div className="skeleton-line skeleton-line-wide" />
    <div className="skeleton-line skeleton-line-short" />
    <div className="skeleton-quote-grid">
      <div className="skeleton-block skeleton-block-hero" />
      <div className="skeleton-block" />
      <div className="skeleton-block" />
    </div>
    <div className="skeleton-metric-grid">
      {Array.from({ length: 5 }, (_, index) => <div className="skeleton-block" key={index} />)}
    </div>
    <div className="skeleton-section" />
    <div className="skeleton-section skeleton-section-chart" />
  </div>
}

const emptyFundFlowPeriod: MainFundFlowPeriod = {
  unit: null,
  main_net_inflow: null,
  main_visible_inflow: null,
  main_hidden_inflow: null,
  retail_inflow: null,
}

const emptyFundFlowValues = {
  today: emptyFundFlowPeriod,
  three_day: emptyFundFlowPeriod,
  five_day: emptyFundFlowPeriod,
}

const fundFlowRows = [
  ['main_net_inflow', '主力净流入'],
  ['main_visible_inflow', '主力明盘'],
  ['main_hidden_inflow', '主力暗盘'],
  ['retail_inflow', '散户流入'],
] as const

const fundFlowColumns = [
  ['today', '当日'],
  ['three_day', '3日'],
  ['five_day', '5日'],
] as const

function formatFundFlowInWan(value: string | null, unit: string | null): string | null {
  if (value === null) return null
  const numericValue = Number(value.replaceAll(',', ''))
  if (!Number.isFinite(numericValue)) return null
  const multiplier = unit === '万元' || unit === '万'
    ? 1
    : unit === '亿元' || unit === '亿' ? 10_000 : null
  if (multiplier === null) return null
  const absoluteWan = Math.abs(numericValue) * multiplier
  const roundedWan = Math.round((absoluteWan + Number.EPSILON) * 100) / 100
  const signedWan = numericValue < 0 ? -roundedWan : roundedWan
  return (Object.is(signedWan, -0) ? 0 : signedWan).toFixed(2)
}

function FundFlowTable({ task, finished, override }: { task: Job, finished: boolean, override?: MainFundFlowValues | null }) {
  const fundFlow = override === undefined ? task.values.main_fund_flow ?? emptyFundFlowValues : override ?? emptyFundFlowValues
  const sources = override === undefined ? task.value_sources?.main_fund_flow : undefined

  return <section className="fund-flow-section" aria-labelledby={`fund-flow-title-${task.public_id}`}>
    <div className="section-heading fund-flow-heading">
      <div className="fund-flow-heading-title">
        <p className="eyebrow">资金增强指标</p>
        <h3 id={`fund-flow-title-${task.public_id}`}>主力流向</h3>
      </div>
      <span className="minor">统一单位：万元</span>
    </div>
    <div className="fund-flow-table-wrap">
      <table className="fund-flow-table">
        <caption className="sr-only">主力流向当日、3日、5日对比，单位万元</caption>
        <thead>
          <tr>
            <th scope="col">指标</th>
            {fundFlowColumns.map(([period, label]) => <th scope="col" key={period}>{label}</th>)}
          </tr>
        </thead>
        <tbody>
          {fundFlowRows.map(([field, label]) => <tr key={field}>
            <th scope="row">{label}</th>
            {fundFlowColumns.map(([period]) => {
              const value = formatFundFlowInWan(
                fundFlow[period]?.[field] ?? null,
                fundFlow[period]?.unit ?? null,
              )
              const displayValue = value ? formatDirectionalValue(value) : null
              const source = sources?.[period]?.[field]
              return <td key={period} className={displayValue ? `market-${displayValue.tone}` : undefined}>
                <span className={displayValue ? `market-${displayValue.tone}` : undefined}>{displayValue?.text ?? (finished ? '未识别' : '待采集')}</span>
                {source === 'OCR' && <small className="ocr-source">OCR识别</small>}
              </td>
            })}
          </tr>)}
        </tbody>
      </table>
    </div>
  </section>
}

type FundFlowChartTab = 'today' | 'daily'
const FUND_FLOW_REFRESH_INTERVAL_MS = 15_000

function fundFlowValuesForDailyPoint(point: FundFlowDailyPoint): MainFundFlowValues | null {
  if (point.periods === null || point.periods === undefined) return null
  return {
    today: point.periods.today ?? emptyFundFlowPeriod,
    three_day: point.periods.three_day ?? emptyFundFlowPeriod,
    five_day: point.periods.five_day ?? emptyFundFlowPeriod,
  }
}

function TaskFundFlowHistory({ history, state, onRetry }: {
  history?: TaskFundFlowHistory
  state: 'idle' | 'loading' | 'ready' | 'empty' | 'error'
  onRetry: () => void
}) {
  return <section className="fund-flow-history-inline" aria-label="当天主力流向历史">
    <div className="section-heading fund-flow-heading">
      <div className="fund-flow-inline-title"><p className="eyebrow">当天资金曲线</p><h3>主力流向趋势</h3></div>
      <span className="minor">{history?.trade_date ?? '当日'}</span>
    </div>
    {state === 'loading' && <p className="fund-flow-chart-status">正在加载当天资金曲线…</p>}
    {state === 'error' && <p className="fund-flow-chart-status error">当天资金曲线加载失败。<button type="button" className="inline-retry" onClick={onRetry}>重新加载</button></p>}
    {state === 'empty' && <p className="fund-flow-chart-status">暂无当天资金曲线。</p>}
    {state === 'ready' && history && <FundFlowHistoryChart period={{ points: history.points }} />}
  </section>
}

function TaskFundFlowDaily({ daily, state, onRetry, onPointChange }: {
  daily?: TaskFundFlowDaily
  state: 'idle' | 'loading' | 'ready' | 'empty' | 'error'
  onRetry: () => void
  onPointChange: (point: FundFlowDailyPoint) => void
}) {
  return <section className="fund-flow-history-inline" aria-label="近30日资金流向">
    <div className="section-heading fund-flow-heading">
      <div className="fund-flow-inline-title"><p className="eyebrow">近30日资金流向</p><h3>收盘资金趋势</h3></div>
      <span className="minor">单位：万元</span>
    </div>
    {state === 'loading' && <p className="fund-flow-chart-status">正在加载近30日资金流向…</p>}
    {state === 'error' && <p className="fund-flow-chart-status error">近30日资金流向加载失败。<button type="button" className="inline-retry" onClick={onRetry}>重新加载</button></p>}
    {state === 'empty' && <p className="fund-flow-chart-status">暂无近30日收盘资金数据。</p>}
    {state === 'ready' && daily && <FundFlowDailyChart points={daily.points} onSelectedPointChange={onPointChange} />}
  </section>
}

function TaskFundFlowTabs({ publicId, finished, hasFundFlow, allowHistoricalFallback, onTabChange, onDailyPointChange }: {
  publicId: string
  finished: boolean
  hasFundFlow: boolean
  allowHistoricalFallback: boolean
  onTabChange: (tab: FundFlowChartTab) => void
  onDailyPointChange: (point: FundFlowDailyPoint) => void
}) {
  const [activeTab, setActiveTab] = useState<FundFlowChartTab>('today')
  const [history, setHistory] = useState<TaskFundFlowHistory>()
  const [daily, setDaily] = useState<TaskFundFlowDaily>()
  const [historyState, setHistoryState] = useState<'idle' | 'loading' | 'ready' | 'empty' | 'error'>('idle')
  const [dailyState, setDailyState] = useState<'idle' | 'loading' | 'ready' | 'empty' | 'error'>('idle')
  const [reloadVersion, setReloadVersion] = useState(0)
  const eligible = (finished || allowHistoricalFallback) && (hasFundFlow || allowHistoricalFallback)

  useEffect(() => {
    if (!eligible) {
      setHistory(undefined)
      setDaily(undefined)
      setHistoryState('idle')
      setDailyState('idle')
      return
    }
    let active = true
    let inFlight = false
    const load = (showLoading: boolean) => {
      if (inFlight) return
      inFlight = true
      if (showLoading) {
        setHistory(undefined)
        setDaily(undefined)
        setHistoryState('loading')
        setDailyState('loading')
      }
      let completed = 0
      const finish = () => {
        completed += 1
        if (completed === 2) inFlight = false
      }
      void api.fundFlowHistory(publicId).then((result) => {
        if (!active) return
        if (Array.isArray(result.points) && result.points.length > 0) {
          setHistory(result)
          setHistoryState('ready')
        } else {
          setHistoryState('empty')
        }
      }).catch((reason) => {
        if (!active) return
        setHistoryState(reason instanceof ApiError && reason.status === 404 ? 'empty' : 'error')
      }).finally(finish)
      void api.fundFlowDaily(publicId, 30).then((result) => {
        if (!active) return
        if (Array.isArray(result.points) && result.points.length > 0) {
          setDaily(result)
          setDailyState('ready')
        } else {
          setDailyState('empty')
        }
      }).catch((reason) => {
        if (!active) return
        setDailyState(reason instanceof ApiError && reason.status === 404 ? 'empty' : 'error')
      }).finally(finish)
    }
    load(true)
    const refreshTimer = window.setInterval(() => load(false), FUND_FLOW_REFRESH_INTERVAL_MS)
    return () => {
      active = false
      window.clearInterval(refreshTimer)
    }
  }, [eligible, publicId, reloadVersion])

  if (!eligible) return null
  const selectTab = (tab: FundFlowChartTab) => {
    setActiveTab(tab)
    onTabChange(tab)
  }
  const todayPanelId = `fund-flow-today-panel-${publicId}`
  const dailyPanelId = `fund-flow-daily-panel-${publicId}`
  return <section className="fund-flow-chart-tabs" aria-label="资金曲线切换">
    <div className="fund-flow-chart-tablist" role="tablist" aria-label="资金曲线类型">
      <button type="button" role="tab" aria-selected={activeTab === 'today'} aria-controls={todayPanelId} className={activeTab === 'today' ? 'active' : ''} onClick={() => selectTab('today')}>当天资金曲线</button>
      <button type="button" role="tab" aria-selected={activeTab === 'daily'} aria-controls={dailyPanelId} className={activeTab === 'daily' ? 'active' : ''} onClick={() => selectTab('daily')}>近30日资金流向</button>
    </div>
    <div id={activeTab === 'today' ? todayPanelId : dailyPanelId} role="tabpanel" aria-label={activeTab === 'today' ? '当天资金曲线内容' : '近30日资金流向内容'}>
      {activeTab === 'today'
        ? <TaskFundFlowHistory history={history} state={historyState} onRetry={() => setReloadVersion((version) => version + 1)} />
        : <TaskFundFlowDaily daily={daily} state={dailyState} onRetry={() => setReloadVersion((version) => version + 1)} onPointChange={onDailyPointChange} />}
    </div>
  </section>
}

function MarketSnapshotResult({ task }: { task: Job }) {
  const [historicalFundFlow, setHistoricalFundFlow] = useState<MainFundFlowValues | null | undefined>()
  const handleFundFlowTabChange = useCallback((tab: FundFlowChartTab) => {
    if (tab === 'today') setHistoricalFundFlow(undefined)
  }, [])
  const handleDailyPointChange = useCallback((point: FundFlowDailyPoint) => {
    setHistoricalFundFlow(fundFlowValuesForDailyPoint(point))
  }, [])
  const snapshot = task.market_snapshot!
  const isLunchBreak = snapshot.market_phase === 'BREAK'
  const snapshotLabel = isLunchBreak ? '上午收盘数据' : '盘后快照'
  const ariaLabel = isLunchBreak ? '午间休市行情' : '盘后行情快照'
  const quote = snapshot.quote ?? {}
  const snapshotFundFlow = {
    today: { ...emptyFundFlowPeriod, ...snapshot.main_fund_flow?.today },
    three_day: { ...emptyFundFlowPeriod, ...snapshot.main_fund_flow?.three_day },
    five_day: { ...emptyFundFlowPeriod, ...snapshot.main_fund_flow?.five_day },
  }
  const snapshotTask: Job = {
    ...task,
    values: {
      ...task.values,
      stock_name: snapshot.name,
      current_price: quote.price ?? quote.current_price ?? null,
      change_percent: quote.change_percent ?? null,
      turnover_rate: quote.turnover_rate ?? null,
      large_order_net: quote.large_order_net ?? null,
      large_order_amount: formatSnapshotLargeOrderAmount(quote.large_order_amount),
      retail_count: quote.retail_count ?? null,
      macdfs: quote.macdfs ?? null,
      main_fund_flow: snapshotFundFlow,
    },
  }
  return <section className="market-snapshot-result" aria-label={ariaLabel}>
    <div className="section-heading">
      <div><p className="eyebrow">MARKET SNAPSHOT</p><h3>{snapshot.name ?? snapshot.symbol} · {snapshotLabel}</h3></div>
      <span className="minor">
        {snapshot.source ?? 'MARKET_DATABASE'}
        {snapshot.source_time ? ` · ${snapshot.source_time}` : ''}
        {snapshot.stale ? ' · 历史' : ''}
      </span>
    </div>
    <QuoteSummary task={snapshotTask} finished />
    <div className="fund-flow-result-layout">
      <FundFlowTable task={snapshotTask} finished override={historicalFundFlow} />
      <TaskFundFlowTabs
        publicId={task.public_id}
        finished
        hasFundFlow
        allowHistoricalFallback
        onTabChange={handleFundFlowTabChange}
        onDailyPointChange={handleDailyPointChange}
      />
    </div>
    {snapshot.stored_trade_dates && <p className="minor market-snapshot-dates">
      大单数据：{snapshot.stored_trade_dates.core_metrics ?? '—'} · 资金流：{snapshot.stored_trade_dates.main_fund_flow ?? '—'}
    </p>}
  </section>
}

function IntradayCharts({ series, publicId }: { series: IntradaySeriesValues, publicId: string }) {
  const charts = [
    ['large_order_net', '大单净量', true, 2],
    ['large_order_amount', '大单金额', true, 2],
    ['retail_count', '散户数量', false, 2],
  ] as const

  const availableCharts = charts.filter(([key]) => series[key].points.length > 0)
  const availableKeySignature = availableCharts.map(([key]) => key).join(',')
  const fallbackKey = availableCharts[0]?.[0] ?? 'large_order_net'
  const [activeKey, setActiveKey] = useState<typeof charts[number][0]>('large_order_net')
  useEffect(() => {
    if (availableCharts.some(([key]) => key === activeKey)) return
    setActiveKey(fallbackKey)
  }, [activeKey, availableKeySignature, fallbackKey])
  if (availableCharts.length === 0) return null
  const activeChart = charts.find(([key]) => key === activeKey) ?? availableCharts[0]
  const chartsId = `intraday-charts-${publicId}`
  const [key, title, directional, precision] = activeChart
  return <section className="intraday-section" aria-labelledby={`intraday-title-${publicId}`}>
    <div className="section-heading intraday-heading">
      <div>
        <p className="eyebrow">App 内部曲线</p>
        <h3 id={`intraday-title-${publicId}`}>当日分时</h3>
      </div>
      <span className="minor">{title}</span>
    </div>
    <div className="intraday-metric-tabs" role="tablist" aria-label="分时指标">
      {charts.map(([chartKey, chartTitle]) => {
        const hasPoints = series[chartKey].points.length > 0
        return <button
          type="button"
          role="tab"
          key={chartKey}
          aria-selected={chartKey === key}
          aria-controls={chartsId}
          disabled={!hasPoints}
          className={`intraday-metric-tab${chartKey === key ? ' active' : ''}`}
          onClick={() => setActiveKey(chartKey)}
        >{chartTitle}</button>
      })}
    </div>
    <div className="intraday-chart-list" id={chartsId} role="tabpanel" aria-label={`${title}分时曲线`}>
      <IntradayMetricChart
        directional={directional}
        precision={precision}
        series={series[key]}
        title={title}
      />
    </div>
  </section>
}

export function JobResult({ task, isLatest = false, onRetry, retrying = false }: {
  task: Job,
  isLatest?: boolean,
  onRetry?: () => void,
  retrying?: boolean,
}) {
  const [historicalFundFlow, setHistoricalFundFlow] = useState<MainFundFlowValues | null | undefined>()
  const handleFundFlowTabChange = useCallback((tab: FundFlowChartTab) => {
    if (tab === 'today') setHistoricalFundFlow(undefined)
  }, [])
  const handleDailyPointChange = useCallback((point: FundFlowDailyPoint) => {
    setHistoricalFundFlow(fundFlowValuesForDailyPoint(point))
  }, [])
  useEffect(() => {
    setHistoricalFundFlow(undefined)
  }, [task.public_id])
  const finished = task.status === 'COMPLETED' || task.status === 'PARTIAL'
  const loading = loadingStatuses.has(task.status)
  const stockDisplayName = task.values.stock_name ? `${task.values.stock_name}（${task.symbol}）` : task.symbol

  return <article className={`job-result${isLatest ? ' latest-job' : ''}`} aria-live="polite" aria-busy={loading}>
    <div className="job-heading">
      <div className="job-identity">
        <div className="job-kicker">
          {isLatest && <span className="latest-badge">最新</span>}
          <span>结果状态</span>
        </div>
        <p className="job-time">更新于 {new Date(task.collected_at ?? task.created_at).toLocaleString('zh-CN')}</p>
      </div>
      <div className="job-heading-actions">
        <span className={`status status-${task.status.toLowerCase()}`}>{taskStatusText(task)}</span>
        {onRetry && <button
          type="button"
          className="secondary job-retry"
          aria-label={`重试 ${task.symbol}`}
          disabled={retrying || !terminalStatuses.has(task.status)}
          onClick={onRetry}
        >
          {retrying ? '重试中…' : '重试'}
        </button>}
        <details className="job-details">
          <summary>任务详情</summary>
          <div className="job-details-body">
            <span>任务 {task.public_id.slice(0, 12)}</span>
            <span>提交时间 {new Date(task.created_at).toLocaleString('zh-CN')}</span>
            {task.collected_at && <span>采集时间 {new Date(task.collected_at).toLocaleString('zh-CN')}</span>}
          </div>
        </details>
      </div>
    </div>

    {task.queue_position != null && task.status === 'QUEUED' && <p className="minor queue-position">当前排队位置：第 {task.queue_position} 位</p>}
    {task.status === 'WAITING_ADMIN' && <p className="notice">设备需要管理员确认，任务会在处理后继续。</p>}
    {task.status === 'FAILED' && <p className="error">{task.error_code ? `错误代码：${task.error_code}` : '请稍后重新提交任务。'}</p>}
    {task.status === 'EXPIRED' && <p className="expired">截图仅保留 24 小时；此任务的下载链接已失效。</p>}

    {loading && <JobResultSkeleton />}

    {!loading && task.status !== 'EXPIRED' && task.status !== 'MARKET_SNAPSHOT' && <>
      <QuoteSummary task={task} finished={finished} />

      <div className="fund-flow-result-layout">
      <FundFlowTable task={task} finished={finished} override={historicalFundFlow} />
      <TaskFundFlowTabs
        publicId={task.public_id}
        finished={finished}
        hasFundFlow={Object.values(task.values.main_fund_flow ?? {}).some((period) => Object.entries(period).some(([key, value]) => key !== 'unit' && value !== null))}
        allowHistoricalFallback={task.status === 'FAILED' && task.error_code === 'DIRECT_PROTOCOL_RESPONSE_TIMEOUT'}
        onTabChange={handleFundFlowTabChange}
        onDailyPointChange={handleDailyPointChange}
      />
      </div>

      {task.values.intraday_series && <IntradayCharts
      publicId={task.public_id}
      series={task.values.intraday_series}
      />}
    </>}
    {task.status === 'MARKET_SNAPSHOT' && task.market_snapshot && <MarketSnapshotResult task={task} />}

  </article>
}

function StockTabs({ tabs, activePublicId, onSelect, onRequestDelete }: {
  tabs: StockTab[]
  activePublicId: string | null
  onSelect: (publicId: string) => void
  onRequestDelete: (publicId: string, trigger: HTMLButtonElement) => void
}) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const [query, setQuery] = useState('')
  const tabRefs = useRef(new Map<string, HTMLButtonElement>())
  const filteredTabs = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return tabs
    return tabs.filter((tab) => `${tab.name} ${tab.symbol}`.toLowerCase().includes(normalized))
  }, [query, tabs])

  useEffect(() => {
    if (activePublicId === null) return
    const activeElement = tabRefs.current.get(activePublicId)
    if (typeof activeElement?.scrollIntoView === 'function') {
      activeElement.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' })
    }
  }, [activePublicId])

  function moveFocus(event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) {
    let nextIndex: number | null = null
    if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = tabs.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    tabRefs.current.get(tabs[nextIndex].public_id)?.focus()
  }

  return <div className="stock-tabs-shell">
    <div className="stock-tabs-toolbar">
      <div className="stock-tab-rail" role="tablist" aria-label="股票页签">
        {tabs.map((tab, index) => <div className="stock-tab-item" role="presentation" key={tab.public_id}>
          <button
            type="button"
            role="tab"
            id={`stock-tab-${tab.public_id}`}
            aria-controls={`stock-panel-${tab.public_id}`}
            aria-selected={tab.public_id === activePublicId}
            className="stock-tab"
            ref={(element) => {
              if (element) tabRefs.current.set(tab.public_id, element)
              else tabRefs.current.delete(tab.public_id)
            }}
            onClick={() => onSelect(tab.public_id)}
            onKeyDown={(event) => moveFocus(event, index)}
          >
            <strong>{tab.name}</strong>
            <span>{tab.symbol}</span>
          </button>
          <button
            type="button"
            className="stock-tab-delete"
            aria-label={`删除 ${tab.name}（${tab.symbol}）页签`}
            onClick={(event) => onRequestDelete(tab.public_id, event.currentTarget)}
          >×</button>
        </div>)}
      </div>
      {tabs.length > 5 && <button
        type="button"
        className="secondary all-stocks-toggle"
        aria-expanded={pickerOpen}
        aria-controls="all-stocks-picker"
        onClick={() => setPickerOpen((open) => !open)}
      >全部股票 {tabs.length}</button>}
    </div>
    {pickerOpen && <div
      className="all-stocks-picker"
      id="all-stocks-picker"
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          setPickerOpen(false)
          setQuery('')
        }
      }}
    >
      <label htmlFor="stock-tab-search">搜索股票页签</label>
      <input
        id="stock-tab-search"
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="输入股票名称或代码"
      />
      <div className="all-stocks-list">
        {filteredTabs.length === 0 ? <p className="minor">没有匹配的股票</p> : filteredTabs.map((tab) => <button
          type="button"
          key={tab.public_id}
          className={tab.public_id === activePublicId ? 'all-stock-option active' : 'all-stock-option'}
          onClick={() => {
            setPickerOpen(false)
            setQuery('')
            onSelect(tab.public_id)
          }}
        >
          <strong>{tab.name}</strong>
          <span>{tab.symbol}</span>
        </button>)}
      </div>
    </div>}
  </div>
}

export default function App({ initialTask }: { initialTask?: Job }) {
  const isAdmin = window.location.hash === '#admin'
  const [symbol, setSymbol] = useState('')
  const [symbolLookup, setSymbolLookup] = useState<SymbolLookupState>({ status: 'idle' })
  const [suggestions, setSuggestions] = useState<SymbolSuggestion[]>([])
  const [suggestionState, setSuggestionState] = useState<SymbolSuggestionState>('idle')
  const [suggestionsOpen, setSuggestionsOpen] = useState(false)
  const [activeSuggestionIndex, setActiveSuggestionIndex] = useState(-1)
  const [composingSymbol, setComposingSymbol] = useState(false)
  const [tabs, setTabs] = useState<StockTab[]>(initialTask ? [{
    public_id: initialTask.public_id,
    symbol: initialTask.symbol,
    name: tabName(initialTask),
    task: initialTask,
  }] : [])
  const [activePublicId, setActivePublicId] = useState<string | null>(initialTask?.public_id ?? null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [restoring, setRestoring] = useState(!initialTask && !isAdmin)
  const [streamState, setStreamState] = useState<JobStreamState | undefined>()
  const [retryingTaskId, setRetryingTaskId] = useState<string | null>(null)
  const [deleteTabId, setDeleteTabId] = useState<string | null>(null)
  const [moreOpen, setMoreOpen] = useState(false)
  const symbolInputRef = useRef<HTMLInputElement>(null)
  const symbolEntryRef = useRef<HTMLDivElement>(null)
  const lookupSequence = useRef(0)
  const suggestionSequence = useRef(0)
  const retryRequests = useRef(new Map<string, Promise<void>>())
  const removedTabIds = useRef(new Set<string>())
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null)
  const deleteCancelRef = useRef<HTMLButtonElement>(null)
  const deleteConfirmRef = useRef<HTMLButtonElement>(null)
  const verifiedSymbol = symbolLookup.status === 'valid' && symbolLookup.result.symbol === symbol
  const activeTaskIds = tabs
    .filter((tab) => !terminalStatuses.has(tab.task.status))
    .map((tab) => tab.public_id)
    .sort()
    .join(',')
  const activeTab = tabs.find((tab) => tab.public_id === activePublicId) ?? tabs[0]
  const deleteTab = tabs.find((tab) => tab.public_id === deleteTabId)

  useEffect(() => {
    if (initialTask || isAdmin) return
    let cancelled = false

    async function restoreHistory() {
      const linkedId = new URLSearchParams(window.location.search).get('job')
      const storedTabs = readStoredTabs()
      const storedById = new Map(storedTabs.map((tab) => [tab.public_id, tab]))
      const ids = [...new Set([
        ...(linkedId ? [linkedId] : []),
        ...storedTabs.map((tab) => tab.public_id),
        ...readHistoryIds(),
      ])].slice(0, MAX_STOCK_TABS)
      if (ids.length === 0) {
        setRestoring(false)
        return
      }

      const results = await Promise.all(ids.map(async (publicId) => {
        try {
          return { publicId, task: await api.getJob(publicId), missing: false, failed: false }
        } catch (reason) {
          return {
            publicId,
            task: undefined,
            missing: reason instanceof ApiError && reason.status === 404,
            failed: !(reason instanceof ApiError && reason.status === 404),
          }
        }
      }))
      if (cancelled) return

      const seenSymbols = new Set<string>()
      const loadedTabs = results.flatMap((result) => {
        if (!result.task || seenSymbols.has(result.task.symbol)) return []
        seenSymbols.add(result.task.symbol)
        const stored = storedById.get(result.publicId)
        return [{
          public_id: result.task.public_id,
          symbol: result.task.symbol,
          name: tabName(result.task, stored?.name),
          task: result.task,
        }]
      })
      persistStockTabs(loadedTabs)
      window.localStorage.removeItem(LEGACY_HISTORY_STORAGE_KEY)
      setTabs(loadedTabs)
      const requestedActive = window.localStorage.getItem(ACTIVE_TAB_STORAGE_KEY)
      const canonicalActive = results.find((result) => result.publicId === requestedActive)?.task?.public_id
      const nextActive = (
        canonicalActive && loadedTabs.some((tab) => tab.public_id === canonicalActive)
          ? canonicalActive
          : linkedId
            ? results.find((result) => result.publicId === linkedId)?.task?.public_id
            : undefined
      ) ?? loadedTabs[0]?.public_id ?? null
      setActivePublicId(nextActive)
      persistActiveTab(nextActive)
      if (results.some((result) => result.failed)) setError('部分本机历史暂时无法加载，请稍后刷新重试。')
      setRestoring(false)
    }

    void restoreHistory()
    return () => { cancelled = true }
  }, [initialTask, isAdmin])

  useEffect(() => {
    if (initialTask || isAdmin) return
    const unsubscribers = (activeTaskIds ? activeTaskIds.split(',') : []).map((publicId) => subscribeToJob(publicId, () => {
      api.getJob(publicId).then((updated) => {
        setTabs((current) => {
          const next = current.map((tab) => tab.public_id === publicId ? {
            ...tab,
            public_id: updated.public_id,
            symbol: updated.symbol,
            name: tabName(updated, tab.name),
            task: updated,
          } : tab)
          persistStockTabs(next)
          return next
        })
        if (activePublicId === publicId && updated.public_id !== publicId) {
          setActivePublicId(updated.public_id)
          persistActiveTab(updated.public_id)
        }
      }).catch((reason) => {
        if (!(reason instanceof ApiError) || reason.status !== 404) return
        setTabs((current) => {
          const next = current.filter((tab) => tab.public_id !== publicId)
          persistStockTabs(next)
          if (activePublicId === publicId) {
            const nextActive = next[0]?.public_id ?? null
            setActivePublicId(nextActive)
            persistActiveTab(nextActive)
          }
          return next
        })
      })
    }, setStreamState))
    return () => unsubscribers.forEach((unsubscribe) => unsubscribe())
  }, [activePublicId, activeTaskIds, initialTask, isAdmin])

  useEffect(() => {
    function closeSuggestions(event: PointerEvent) {
      if (!symbolEntryRef.current?.contains(event.target as Node)) {
        setSuggestionsOpen(false)
        setActiveSuggestionIndex(-1)
      }
    }
    document.addEventListener('pointerdown', closeSuggestions)
    return () => document.removeEventListener('pointerdown', closeSuggestions)
  }, [])

  useEffect(() => {
    if (deleteTabId !== null) deleteCancelRef.current?.focus()
  }, [deleteTabId])

  useEffect(() => {
    const normalized = symbol.trim()
    const numericInput = /^\d+$/.test(normalized)
    if (
      isAdmin
      || composingSymbol
      || /^\d{6}$/.test(normalized)
      || numericInput
      || normalized.length < 2
      || normalized.length > 32
    ) {
      suggestionSequence.current += 1
      setSuggestions([])
      setSuggestionState('idle')
      setSuggestionsOpen(false)
      setActiveSuggestionIndex(-1)
      return
    }

    const sequence = ++suggestionSequence.current
    const controller = new AbortController()
    setSuggestionState('loading')
    setSuggestionsOpen(true)
    setActiveSuggestionIndex(-1)
    const timer = window.setTimeout(() => {
      api.searchSymbols(normalized, controller.signal).then((results) => {
        if (controller.signal.aborted || suggestionSequence.current !== sequence) return
        setSuggestions(results)
        setSuggestionState(results.length === 0 ? 'empty' : 'ready')
        setSuggestionsOpen(true)
        setActiveSuggestionIndex(-1)
      }).catch((reason) => {
        if (controller.signal.aborted || suggestionSequence.current !== sequence) return
        setSuggestions([])
        setSuggestionState('unavailable')
        setSuggestionsOpen(true)
        setActiveSuggestionIndex(-1)
      })
    }, 300)

    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [composingSymbol, isAdmin, symbol])

  useEffect(() => {
    if (isAdmin || !/^\d{6}$/.test(symbol)) {
      lookupSequence.current += 1
      setSymbolLookup({ status: 'idle' })
      return
    }

    const sequence = ++lookupSequence.current
    const controller = new AbortController()
    setSymbolLookup({ status: 'loading' })
    api.lookupSymbol(symbol, controller.signal).then((result) => {
      if (controller.signal.aborted || lookupSequence.current !== sequence || result.symbol !== symbol) return
      setSymbolLookup({ status: 'valid', result })
    }).catch((reason) => {
      if (controller.signal.aborted || lookupSequence.current !== sequence) return
      if (reason instanceof ApiError && (reason.status === 404 || reason.status === 409)) {
        setSymbolLookup({ status: 'invalid' })
      } else {
        setSymbolLookup({ status: 'unavailable' })
      }
    })

    return () => controller.abort()
  }, [isAdmin, symbol])

  function placeTabAtTop(updated: Job, preferredName?: string) {
    setTabs((current) => {
      const existing = current.find((tab) => tab.public_id === updated.public_id || tab.symbol === updated.symbol)
      const nextTab: StockTab = {
        public_id: updated.public_id,
        symbol: updated.symbol,
        name: tabName(updated, preferredName || existing?.name),
        task: updated,
      }
      const next = [nextTab, ...current.filter((tab) => tab.public_id !== updated.public_id && tab.symbol !== updated.symbol)].slice(0, MAX_STOCK_TABS)
      persistStockTabs(next)
      return next
    })
    setActivePublicId(updated.public_id)
    persistActiveTab(updated.public_id)
  }

  function moveTabToTop(publicId: string) {
    setTabs((current) => {
      const selected = current.find((tab) => tab.public_id === publicId)
      if (!selected) return current
      const next = [selected, ...current.filter((tab) => tab.public_id !== publicId)]
      persistStockTabs(next)
      return next
    })
    setActivePublicId(publicId)
    persistActiveTab(publicId)
  }

  function retryTask(publicId: string): Promise<void> {
    const pending = retryRequests.current.get(publicId)
    if (pending) return pending
    setRetryingTaskId(publicId)
    setError('')
    const request = api.retryJob(publicId).then((updated) => {
      if (!removedTabIds.current.has(publicId)) placeTabAtTop(updated)
    }).catch((reason) => {
      setError(reason instanceof Error ? reason.message : '重试失败，请稍后重试。')
    }).finally(() => {
      retryRequests.current.delete(publicId)
      setRetryingTaskId((current) => current === publicId ? null : current)
    })
    retryRequests.current.set(publicId, request)
    return request
  }

  function selectTab(publicId: string) {
    const isCurrent = publicId === activePublicId
    moveTabToTop(publicId)
    if (!isCurrent) void retryTask(publicId)
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!verifiedSymbol || symbolLookup.status !== 'valid') {
      setError('请先输入 6 位股票代码并等待名称确认。')
      return
    }
    const normalized = symbolLookup.result.symbol
    setSubmitting(true)
    setError('')
    try {
      const nextTask = await api.submitJob(normalized, false)
      removedTabIds.current.delete(nextTask.public_id)
      placeTabAtTop(nextTask, symbolLookup.result.name)
      setSymbol('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '提交失败，请稍后重试。')
    } finally {
      setSubmitting(false)
    }
  }

  function chooseSuggestion(suggestion: SymbolSuggestion) {
    suggestionSequence.current += 1
    setSuggestionsOpen(false)
    setSuggestions([])
    setSuggestionState('idle')
    setActiveSuggestionIndex(-1)
    setSymbol(suggestion.symbol)
    setError('')
  }

  function handleSymbolKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape' && suggestionsOpen) {
      event.preventDefault()
      setSuggestionsOpen(false)
      setActiveSuggestionIndex(-1)
      return
    }
    if (!suggestionsOpen || suggestionState !== 'ready' || suggestions.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveSuggestionIndex((current) => (current + 1 + suggestions.length) % suggestions.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveSuggestionIndex((current) => (current - 1 + suggestions.length) % suggestions.length)
    } else if (event.key === 'Enter' && activeSuggestionIndex >= 0) {
      event.preventDefault()
      chooseSuggestion(suggestions[activeSuggestionIndex])
    }
  }

  function changeSymbol(value: string) {
    setSymbol(value.slice(0, 32))
    setError('')
  }

  function clearHistory() {
    if (!window.confirm('确定清空当前浏览器中的采集记录吗？服务器上的任务保留规则不会改变。')) return
    window.localStorage.removeItem(LEGACY_HISTORY_STORAGE_KEY)
    window.localStorage.removeItem(STOCK_TABS_STORAGE_KEY)
    window.localStorage.removeItem(ACTIVE_TAB_STORAGE_KEY)
    setTabs([])
    setActivePublicId(null)
    setStreamState(undefined)
    window.history.replaceState({}, '', `${window.location.pathname}${window.location.hash}`)
  }

  function requestTabDelete(publicId: string, trigger: HTMLButtonElement) {
    deleteTriggerRef.current = trigger
    setDeleteTabId(publicId)
  }

  function cancelTabDelete() {
    const trigger = deleteTriggerRef.current
    setDeleteTabId(null)
    window.requestAnimationFrame(() => trigger?.focus())
  }

  function confirmTabDelete() {
    if (deleteTabId === null) return
    const removedIndex = tabs.findIndex((tab) => tab.public_id === deleteTabId)
    if (removedIndex < 0) {
      setDeleteTabId(null)
      return
    }
    const nextTabs = tabs.filter((tab) => tab.public_id !== deleteTabId)
    let nextActive = activePublicId
    if (activePublicId === deleteTabId) {
      nextActive = nextTabs[removedIndex]?.public_id
        ?? nextTabs[removedIndex - 1]?.public_id
        ?? null
    }
    removedTabIds.current.add(deleteTabId)
    retryRequests.current.delete(deleteTabId)
    setRetryingTaskId((current) => current === deleteTabId ? null : current)
    setTabs(nextTabs)
    persistStockTabs(nextTabs)
    setActivePublicId(nextActive)
    persistActiveTab(nextActive)
    setDeleteTabId(null)
    if (nextActive !== null) {
      window.requestAnimationFrame(() => {
        document.getElementById(`stock-tab-${nextActive}`)?.focus()
      })
    }
  }

  function handleDeleteDialogKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      cancelTabDelete()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = [deleteCancelRef.current, deleteConfirmRef.current].filter(
      (item): item is HTMLButtonElement => item !== null,
    )
    if (focusable.length === 0) return
    const currentIndex = focusable.indexOf(document.activeElement as HTMLButtonElement)
    const nextIndex = event.shiftKey
      ? (currentIndex - 1 + focusable.length) % focusable.length
      : (currentIndex + 1) % focusable.length
    event.preventDefault()
    focusable[nextIndex].focus()
  }

  if (isAdmin) return <AdminPage autoLoad />

  return <main className="page-shell">
    <header className="query-header">
      <h1 className="sr-only">股票数据查询</h1>
      <div className="more-menu">
        <button type="button" aria-expanded={moreOpen} aria-controls="more-menu-panel" onClick={() => setMoreOpen((open) => !open)}>更多</button>
        {moreOpen && <nav className="more-menu-panel" id="more-menu-panel" aria-label="辅助入口">
          <a href="/market">进入行情中心</a>
          <a href="/#admin">管理台</a>
          {!initialTask && tabs.length > 0 && <button type="button" className="menu-danger" onClick={clearHistory}>清空本机记录</button>}
        </nav>}
      </div>
    </header>

    <section className="panel submit-panel" aria-label="提交采集任务">
      <form onSubmit={submit}>
        <label className="sr-only" htmlFor="symbol">股票代码或名称</label>
        <div className="form-row">
          <div className="symbol-entry-field" ref={symbolEntryRef}>
            <input
              ref={symbolInputRef}
              id="symbol"
              name="stock-symbol"
              type="text"
              inputMode="search"
              autoComplete="off"
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={suggestionsOpen}
              aria-controls="symbol-suggestion-list"
              aria-activedescendant={activeSuggestionIndex >= 0 ? `symbol-suggestion-${activeSuggestionIndex}` : undefined}
              aria-describedby="symbol-lookup-status"
              value={symbol}
              onChange={(event) => changeSymbol(event.target.value)}
              onKeyDown={handleSymbolKeyDown}
              onCompositionStart={() => setComposingSymbol(true)}
              onCompositionEnd={(event) => {
                setComposingSymbol(false)
                changeSymbol(event.currentTarget.value)
              }}
              onFocus={() => {
                if (suggestionState !== 'idle') setSuggestionsOpen(true)
              }}
              placeholder="输入代码或名称，例如 国盾"
              maxLength={32}
            />
            {suggestionsOpen && <div
              className="symbol-suggestion-list"
              id="symbol-suggestion-list"
              role="listbox"
              aria-label="股票候选"
            >
              {suggestionState === 'loading' && <p role="status">正在搜索股票…</p>}
              {suggestionState === 'empty' && <p>没有匹配的股票</p>}
              {suggestionState === 'unavailable' && <p>股票候选查询暂时不可用</p>}
              {suggestionState === 'ready' && suggestions.map((suggestion, index) => <button
                type="button"
                role="option"
                aria-selected={index === activeSuggestionIndex}
                id={`symbol-suggestion-${index}`}
                key={`${suggestion.symbol}-${suggestion.market}`}
                className="symbol-suggestion-option"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => chooseSuggestion(suggestion)}
              >
                <strong>{suggestion.name}</strong>
                <span>{suggestion.symbol}</span>
                <small>{suggestion.market_label || suggestion.market}</small>
              </button>)}
            </div>}
          </div>
          <button type="submit" aria-label="提交采集任务" disabled={submitting || !verifiedSymbol}>{submitting ? '提交中…' : '查询'}</button>
        </div>
        <div
          id="symbol-lookup-status"
          className={`symbol-lookup symbol-lookup-${symbolLookup.status}`}
          aria-live="polite"
        >
          {symbolLookup.status === 'idle' && suggestionState === 'idle' && <span>输入六位代码，或至少两个字搜索股票名称</span>}
          {symbolLookup.status === 'idle' && suggestionState === 'ready' && <span>选择候选后将进行六位代码精确确认</span>}
          {symbolLookup.status === 'loading' && <span>正在查询股票…</span>}
          {symbolLookup.status === 'invalid' && <span>未找到该股票</span>}
          {symbolLookup.status === 'unavailable' && <span>股票查询暂时不可用</span>}
          {symbolLookup.status === 'valid' && <>
            <strong className="symbol-lookup-value">{symbolLookup.result.name}（{symbolLookup.result.symbol}）</strong>
            <button
              type="button"
              className="symbol-edit"
              onClick={() => {
                symbolInputRef.current?.focus()
                symbolInputRef.current?.select()
              }}
            >修改代码</button>
          </>}
        </div>
      </form>
      {error && <p className="error form-error" role="alert">{error}</p>}
    </section>

    <section className="history-section" aria-labelledby="history-title">
      <div className="history-heading">
        <div>
          <p className="eyebrow">当前浏览器</p>
          <h2 id="history-title">股票页签</h2>
          <p>股票和指标结果会永久保存在当前浏览器。</p>
        </div>
      </div>

      {restoring ? <div className="history-empty" role="status">正在恢复本机记录…</div> : tabs.length === 0 ? <div className="history-empty">
        <strong>还没有采集记录</strong>
        <p>提交第一个股票代码后，结果会显示在这里。</p>
      </div> : <div className="stock-tab-workspace">
        <StockTabs
          tabs={tabs}
          activePublicId={activeTab?.public_id ?? null}
          onSelect={selectTab}
          onRequestDelete={requestTabDelete}
        />
        {activeTab && <div
          className="stock-tab-panel"
          role="tabpanel"
          id={`stock-panel-${activeTab.public_id}`}
          aria-labelledby={`stock-tab-${activeTab.public_id}`}
        >
          <JobResult
            key={activeTab.public_id}
            task={activeTab.task}
            isLatest
            onRetry={() => void retryTask(activeTab.public_id)}
            retrying={retryingTaskId === activeTab.public_id}
          />
        </div>}
      </div>}
    </section>

    {streamState === 'RECONNECTING' && <p className="notice stream-notice" role="status">任务状态流暂时断开，正在自动重连。</p>}
    {deleteTab && <div className="tab-delete-overlay" onMouseDown={(event) => {
      if (event.target === event.currentTarget) cancelTabDelete()
    }}>
      <div
        className="tab-delete-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tab-delete-title"
        aria-describedby="tab-delete-description"
        onKeyDown={handleDeleteDialogKeyDown}
      >
        <p className="eyebrow">当前浏览器</p>
        <h2 id="tab-delete-title">删除股票页签</h2>
        <p id="tab-delete-description">确定从当前浏览器删除 {deleteTab.name}（{deleteTab.symbol}）吗？</p>
        <p className="minor">服务端任务和其他浏览器不会受到影响。</p>
        <div className="tab-delete-actions">
          <button type="button" className="secondary" ref={deleteCancelRef} onClick={cancelTabDelete}>取消</button>
          <button type="button" className="tab-delete-confirm" ref={deleteConfirmRef} onClick={confirmTabDelete}>删除页签</button>
        </div>
      </div>
    </div>}
    <footer>仅采集已登录设备中可正常访问的页面，不绕过验证或权限限制。</footer>
  </main>
}
