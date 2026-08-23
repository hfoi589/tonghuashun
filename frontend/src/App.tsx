import { FormEvent, useEffect, useRef, useState } from 'react'
import { AdminPage } from './AdminPage'
import { ApiError, api, type IntradaySeriesValues, type Job, type JobStatus, type JobStreamState, type MainFundFlowPeriod, type SymbolLookup, type ValueSource, subscribeToJob } from './api'
import { IntradayMetricChart } from './IntradayMetricChart'
import './styles.css'

const HISTORY_STORAGE_KEY = 'ths_level2_job_history'
const HISTORY_LIMIT = 50
const terminalStatuses = new Set<JobStatus>(['COMPLETED', 'PARTIAL', 'FAILED', 'EXPIRED'])

type SymbolLookupState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'valid', result: SymbolLookup }
  | { status: 'invalid' }
  | { status: 'unavailable' }

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
    const value = JSON.parse(window.localStorage.getItem(HISTORY_STORAGE_KEY) ?? '[]')
    if (!Array.isArray(value)) return []
    return [...new Set(value.filter((item): item is string => (
      typeof item === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(item)
    )))].slice(0, HISTORY_LIMIT)
  } catch {
    return []
  }
}

function persistHistoryIds(ids: string[]): void {
  const unique = [...new Set(ids)].slice(0, HISTORY_LIMIT)
  if (unique.length === 0) {
    window.localStorage.removeItem(HISTORY_STORAGE_KEY)
    return
  }
  window.localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(unique))
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
  const charts = [
    ['large_order_net', '大单净量', true, 2],
    ['large_order_amount', '大单金额', true, 1],
    ['retail_count', '散户数量', false, 2],
  ] as const

  if (!charts.some(([key]) => series[key].points.length > 0)) return null
  return <section className="intraday-section" aria-labelledby={`intraday-title-${publicId}`}>
    <div className="section-heading intraday-heading">
      <div>
        <p className="eyebrow">App 内部曲线</p>
        <h3 id={`intraday-title-${publicId}`}>当日分时</h3>
      </div>
      <span className="minor">悬停或使用左右键查看每个时点</span>
    </div>
    <div className="intraday-chart-list">
      {charts.map(([key, title, directional, precision]) => <IntradayMetricChart
        directional={directional}
        key={key}
        precision={precision}
        series={series[key]}
        title={title}
      />)}
    </div>
  </section>
}

export function JobResult({ task, isLatest = false }: { task: Job, isLatest?: boolean }) {
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
      <span className={`status status-${task.status.toLowerCase()}`}>{taskStatusText(task)}</span>
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
    </section> : task.status !== 'EXPIRED' && (
      task.long_capture.status === 'SKIPPED' || !task.include_long_capture
        ? <p className="minor long-capture-skipped">未请求长截图</p>
        : <p className="minor long-capture-pending">整页长截图尚未生成。</p>
    )}
  </article>
}

export default function App({ initialTask }: { initialTask?: Job }) {
  const isAdmin = window.location.hash === '#admin'
  const [symbol, setSymbol] = useState('')
  const [symbolLookup, setSymbolLookup] = useState<SymbolLookupState>({ status: 'idle' })
  const [includeLongCapture, setIncludeLongCapture] = useState(false)
  const [tasks, setTasks] = useState<Job[]>(initialTask ? [initialTask] : [])
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [restoring, setRestoring] = useState(!initialTask && !isAdmin)
  const [streamState, setStreamState] = useState<JobStreamState | undefined>()
  const symbolInputRef = useRef<HTMLInputElement>(null)
  const lookupSequence = useRef(0)
  const verifiedSymbol = symbolLookup.status === 'valid' && symbolLookup.result.symbol === symbol
  const activeTaskIds = tasks
    .filter((task) => !terminalStatuses.has(task.status))
    .map((task) => task.public_id)
    .join(',')

  useEffect(() => {
    if (initialTask || isAdmin) return
    let cancelled = false

    async function restoreHistory() {
      const linkedId = new URLSearchParams(window.location.search).get('job')
      const ids = [...new Set([...(linkedId ? [linkedId] : []), ...readHistoryIds()])].slice(0, HISTORY_LIMIT)
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

      const loaded = results.flatMap((result) => result.task ? [result.task] : [])
      const retainedIds = results.filter((result) => !result.missing).map((result) => result.publicId)
      persistHistoryIds(retainedIds)
      setTasks((current) => [
        ...current,
        ...loaded.filter((task) => !current.some((item) => item.public_id === task.public_id)),
      ].slice(0, HISTORY_LIMIT))
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
        setTasks((current) => current.map((item) => item.public_id === updated.public_id ? updated : item))
      }).catch((reason) => {
        if (!(reason instanceof ApiError) || reason.status !== 404) return
        setTasks((current) => {
          const next = current.filter((item) => item.public_id !== publicId)
          persistHistoryIds(next.map((item) => item.public_id))
          return next
        })
      })
    }, setStreamState))
    return () => unsubscribers.forEach((unsubscribe) => unsubscribe())
  }, [activeTaskIds, initialTask, isAdmin])

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
      setTasks((current) => {
        const next = [nextTask, ...current.filter((item) => item.public_id !== nextTask.public_id)].slice(0, HISTORY_LIMIT)
        persistHistoryIds(next.map((item) => item.public_id))
        return next
      })
      setSymbol('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '提交失败，请稍后重试。')
    } finally {
      setSubmitting(false)
    }
  }

  function changeSymbol(value: string) {
    setSymbol(value.replace(/\D/g, '').slice(0, 6))
    setError('')
  }

  function clearHistory() {
    if (!window.confirm('确定清空当前浏览器中的采集记录吗？服务器上的任务保留规则不会改变。')) return
    window.localStorage.removeItem(HISTORY_STORAGE_KEY)
    setTasks([])
    setStreamState(undefined)
    window.history.replaceState({}, '', `${window.location.pathname}${window.location.hash}`)
  }

  if (isAdmin) return <AdminPage />

  return <main className="page-shell">
    <header className="hero">
      <p className="eyebrow">THS LEVEL2</p>
      <h1>同花顺数据采集</h1>
    </header>

    <section className="panel submit-panel" aria-label="提交采集任务">
      <form onSubmit={submit}>
        <label className="sr-only" htmlFor="symbol">股票代码</label>
        <div className="form-row">
          <input
            ref={symbolInputRef}
            id="symbol"
            name="stock-symbol"
            type="text"
            inputMode="numeric"
            pattern="[0-9]{6}"
            autoComplete="off"
            aria-describedby="symbol-lookup-status"
            value={symbol}
            onChange={(event) => changeSymbol(event.target.value)}
            placeholder="例如 600938"
            maxLength={6}
          />
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
          {symbolLookup.status === 'idle' && <span>输入满 6 位后自动查询股票名称</span>}
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
          <h2 id="history-title">采集历史</h2>
          <p>任务信息最多可恢复 7 天，长截图链接保留 24 小时。</p>
        </div>
        {!initialTask && tasks.length > 0 && <button type="button" className="secondary clear-history" onClick={clearHistory}>清空本机记录</button>}
      </div>

      {restoring ? <div className="history-empty" role="status">正在恢复本机记录…</div> : tasks.length === 0 ? <div className="history-empty">
        <strong>还没有采集记录</strong>
        <p>提交第一个股票代码后，结果会显示在这里。</p>
      </div> : <div className="history-list">
        {tasks.map((task, index) => <JobResult key={task.public_id} task={task} isLatest={index === 0} />)}
      </div>}
    </section>

    {streamState === 'RECONNECTING' && <p className="notice stream-notice" role="status">任务状态流暂时断开，正在自动重连。</p>}
    <footer>仅采集已登录设备中可正常访问的页面，不绕过验证或权限限制。</footer>
  </main>
}
