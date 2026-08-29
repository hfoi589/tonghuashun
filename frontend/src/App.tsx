import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { AdminPage } from './AdminPage'
import { ApiError, api, type IntradaySeriesValues, type Job, type JobStatus, type JobStreamState, type MainFundFlowPeriod, type SymbolLookup, type SymbolSuggestion, type ValueSource, subscribeToJob } from './api'
import { IntradayMetricChart } from './IntradayMetricChart'
import './styles.css'

const LEGACY_HISTORY_STORAGE_KEY = 'ths_level2_job_history'
const STOCK_TABS_STORAGE_KEY = 'ths_level2_stock_tabs_v2'
const ACTIVE_TAB_STORAGE_KEY = 'ths_level2_active_stock_tab'
const MAX_STOCK_TABS = 50
const terminalStatuses = new Set<JobStatus>(['COMPLETED', 'PARTIAL', 'FAILED', 'EXPIRED'])

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
  COMPLETED: '数据和长截图已就绪',
  PARTIAL: '长截图已生成，部分数据未读取',
  FAILED: '采集未完成',
  EXPIRED: '结果已过期',
}

function taskStatusText(task: Job): string {
  if (!task.include_long_capture && task.status === 'COMPLETED') return '数据已就绪'
  if (!task.include_long_capture && task.status === 'PARTIAL') return '部分数据未读取'
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

const emptyFundFlowPeriod: MainFundFlowPeriod = {
  unit: null,
  main_net_inflow: null,
  main_visible_inflow: null,
  main_hidden_inflow: null,
  retail_inflow: null,
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

function formatFundFlowInYi(value: string | null, unit: string | null): string | null {
  if (value === null) return null
  const numericValue = Number(value.replaceAll(',', ''))
  if (!Number.isFinite(numericValue)) return null
  const divisor = unit === '万元' || unit === '万'
    ? 10_000
    : unit === '亿元' || unit === '亿' ? 1 : null
  if (divisor === null) return null
  const absoluteYi = Math.abs(numericValue) / divisor
  const roundedYi = Math.round((absoluteYi + Number.EPSILON) * 100) / 100
  const signedYi = numericValue < 0 ? -roundedYi : roundedYi
  return (Object.is(signedYi, -0) ? 0 : signedYi).toFixed(2)
}

function FundFlowTable({ task, finished }: { task: Job, finished: boolean }) {
  const fundFlow = task.values.main_fund_flow ?? {
    today: emptyFundFlowPeriod,
    three_day: emptyFundFlowPeriod,
    five_day: emptyFundFlowPeriod,
  }
  const sources = task.value_sources?.main_fund_flow

  return <section className="fund-flow-section" aria-labelledby={`fund-flow-title-${task.public_id}`}>
    <div className="section-heading fund-flow-heading">
      <div>
        <p className="eyebrow">资金增强指标</p>
        <h3 id={`fund-flow-title-${task.public_id}`}>主力流向</h3>
      </div>
      <span className="minor">统一单位：亿元</span>
    </div>
    <div className="fund-flow-table-wrap">
      <table className="fund-flow-table">
        <caption className="sr-only">主力流向当日、3日、5日对比，单位亿元</caption>
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
              const value = formatFundFlowInYi(
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

function IntradayCharts({ series, publicId }: { series: IntradaySeriesValues, publicId: string }) {
  const [open, setOpen] = useState(false)
  const charts = [
    ['large_order_net', '大单净量', true, 2],
    ['large_order_amount', '大单金额', true, 1],
    ['retail_count', '散户数量', false, 2],
  ] as const

  if (!charts.some(([key]) => series[key].points.length > 0)) return null
  const chartsId = `intraday-charts-${publicId}`
  return <section className="intraday-section" aria-labelledby={`intraday-title-${publicId}`}>
    <div className="section-heading intraday-heading">
      <div>
        <p className="eyebrow">App 内部曲线</p>
        <h3 id={`intraday-title-${publicId}`}>当日分时</h3>
      </div>
      <button
        type="button"
        className="secondary intraday-toggle"
        aria-controls={chartsId}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        {open ? '收起曲线' : '展开曲线'}
      </button>
    </div>
    {open && <div className="intraday-chart-list" id={chartsId}>
      {charts.map(([key, title, directional, precision]) => <IntradayMetricChart
          directional={directional}
          key={key}
          precision={precision}
          series={series[key]}
          title={title}
        />)}
      </div>}
  </section>
}

export function JobResult({ task, isLatest = false, onRetry, retrying = false }: {
  task: Job,
  isLatest?: boolean,
  onRetry?: () => void,
  retrying?: boolean,
}) {
  const [captureOpen, setCaptureOpen] = useState(false)
  const finished = task.status === 'COMPLETED' || task.status === 'PARTIAL'
  const captureReady = task.long_capture.status === 'READY' && Boolean(task.long_capture.url)
  const captureId = `long-capture-${task.public_id}`
  const stockDisplayName = task.values.stock_name ? `${task.values.stock_name}（${task.symbol}）` : null

  return <article className={`job-result${isLatest ? ' latest-job' : ''}`} aria-live="polite">
    <div className="job-heading">
      <div className="job-identity">
        <div className="job-kicker">
          {isLatest && <span className="latest-badge">最新</span>}
          <span>任务 {task.public_id.slice(0, 12)}</span>
        </div>
        <p className="job-time">提交时间 {new Date(task.created_at).toLocaleString('zh-CN')}</p>
        {task.collected_at && <p className="job-time">采集时间 {new Date(task.collected_at).toLocaleString('zh-CN')}</p>}
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
      </div>
    </div>

    {task.queue_position != null && task.status === 'QUEUED' && <p className="minor queue-position">当前排队位置：第 {task.queue_position} 位</p>}
    {task.status === 'WAITING_ADMIN' && <p className="notice">设备需要管理员确认，任务会在处理后继续。</p>}
    {task.status === 'FAILED' && <p className="error">{task.error_code ? `错误代码：${task.error_code}` : '请稍后重新提交任务。'}</p>}
    {task.status === 'EXPIRED' && <p className="expired">截图仅保留 24 小时；此任务的下载链接已失效。</p>}

    {task.status !== 'EXPIRED' && <div className="value-grid">
      <ValueCard name="股票名称" value={stockDisplayName} source={task.value_sources?.stock_name} finished={finished} />
      <ValueCard name="当前股价" value={task.values.current_price} source={task.value_sources?.current_price} finished={finished} />
      <ValueCard name="当前涨跌幅" value={task.values.change_percent} source={task.value_sources?.change_percent} finished={finished} directional />
      <ValueCard name="换手率" value={task.values.turnover_rate} source={task.value_sources?.turnover_rate} finished={finished} />
      <ValueCard name="大单净量" value={task.values.large_order_net} source={task.value_sources?.large_order_net} finished={finished} directional />
      <ValueCard name="大单金额" value={task.values.large_order_amount} source={task.value_sources?.large_order_amount} finished={finished} directional />
      <ValueCard name="散户数量" value={task.values.retail_count} source={task.value_sources?.retail_count} finished={finished} directional />
      <ValueCard name="MACDFS" value={task.values.macdfs} source={task.value_sources?.macdfs} finished={finished} directional />
    </div>}

    {task.status !== 'EXPIRED' && <FundFlowTable task={task} finished={finished} />}

    {task.status !== 'EXPIRED' && task.values.intraday_series && <IntradayCharts
      publicId={task.public_id}
      series={task.values.intraday_series}
    />}

    {captureReady ? <section className="capture-drawer">
      <button
        type="button"
        className="capture-toggle"
        aria-label={captureOpen ? '收起长截图' : '展开长截图'}
        aria-expanded={captureOpen}
        aria-controls={captureId}
        onClick={() => setCaptureOpen((open) => !open)}
      >
        <span>
          <strong>整页长截图</strong>
          <small>{task.long_capture.expires_at ? `可用至 ${new Date(task.long_capture.expires_at).toLocaleString('zh-CN')}` : '可在有效期内查看'}</small>
        </span>
        <span className="capture-toggle-label">{captureOpen ? '收起长截图' : '展开长截图'}</span>
      </button>
      {captureOpen && <div className="capture-drawer-body" id={captureId}>
        <div className="capture-actions">
          <a href={task.long_capture.url!} target="_blank" rel="noreferrer">打开长截图</a>
          <a href={task.long_capture.url!} download>下载长截图</a>
        </div>
        <img src={task.long_capture.url!} alt={`${task.symbol} Level2 整页长截图`} loading="lazy" />
      </div>}
    </section> : task.long_capture.status === 'EXPIRED'
      ? <p className="minor long-capture-expired">长截图已过期，指标数据仍保留</p>
      : task.status !== 'EXPIRED' && (
        task.long_capture.status === 'SKIPPED' || !task.include_long_capture
          ? <p className="minor long-capture-skipped">未请求长截图</p>
          : <p className="minor long-capture-pending">整页长截图尚未生成。</p>
      )}
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
  const [includeLongCapture, setIncludeLongCapture] = useState(false)
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
    moveTabToTop(publicId)
    void retryTask(publicId)
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
      const nextTask = await api.submitJob(normalized, includeLongCapture)
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

  if (isAdmin) return <AdminPage />

  return <main className="page-shell">
    <header className="hero">
      <p className="eyebrow">THS LEVEL2</p>
      <h1>同花顺数据采集</h1>
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
          <button type="submit" disabled={submitting || !verifiedSymbol}>{submitting ? '正在提交…' : '提交采集任务'}</button>
          <label className="capture-option">
            <input
              type="checkbox"
              aria-label="生成整页长截图"
              checked={includeLongCapture}
              onChange={(event) => setIncludeLongCapture(event.target.checked)}
            />
            <span><strong>生成整页长截图</strong></span>
          </label>
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
          <p>股票和指标结果会永久保存在当前浏览器；长截图链接保留 24 小时。</p>
        </div>
        {!initialTask && tabs.length > 0 && <button type="button" className="secondary clear-history" onClick={clearHistory}>清空本机记录</button>}
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
