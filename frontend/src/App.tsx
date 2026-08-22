import { FormEvent, useEffect, useRef, useState } from 'react'
import { AdminPage } from './AdminPage'
import { ApiError, api, type Job, type JobStatus, type JobStreamState, type SymbolLookup, type ValueSource, subscribeToJob } from './api'
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

export function JobResult({ task, isLatest = false }: { task: Job, isLatest?: boolean }) {
  const [captureOpen, setCaptureOpen] = useState(false)
  const finished = task.status === 'COMPLETED' || task.status === 'PARTIAL'
  const captureReady = task.long_capture.status === 'READY' && Boolean(task.long_capture.url)
  const captureId = `long-capture-${task.public_id}`

  return <article className={`job-result${isLatest ? ' latest-job' : ''}`} aria-live="polite">
    <div className="job-heading">
      <div className="job-identity">
        <div className="job-kicker">
          {isLatest && <span className="latest-badge">最新</span>}
          <span>任务 {task.public_id.slice(0, 12)}</span>
        </div>
        <h3>{task.values.stock_name ?? task.symbol}</h3>
        {task.values.stock_name && <p className="job-symbol">{task.symbol}</p>}
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
      <ValueCard name="股票名称" value={task.values.stock_name} source={task.value_sources?.stock_name} finished={finished} />
      <ValueCard name="当前股价" value={task.values.current_price} source={task.value_sources?.current_price} finished={finished} />
      <ValueCard name="当前涨跌幅" value={task.values.change_percent} source={task.value_sources?.change_percent} finished={finished} directional />
      <ValueCard name="换手率" value={task.values.turnover_rate} source={task.value_sources?.turnover_rate} finished={finished} />
      <ValueCard name="大单净量" value={task.values.large_order_net} source={task.value_sources?.large_order_net} finished={finished} directional />
      <ValueCard name="大单金额" value={task.values.large_order_amount} source={task.value_sources?.large_order_amount} finished={finished} directional />
      <ValueCard name="散户数量" value={task.values.retail_count} source={task.value_sources?.retail_count} finished={finished} directional />
      <ValueCard name="MACDFS" value={task.values.macdfs} source={task.value_sources?.macdfs} finished={finished} directional />
    </div>}

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
  const [includeLongCapture, setIncludeLongCapture] = useState(true)
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
      <h1>采集 Level2 数据，结果逐条保留</h1>
      <p>输入股票代码后进入单设备队列。每次采集都会追加到当前浏览器的历史记录，不再覆盖上一条结果。</p>
    </header>

    <section className="panel submit-panel" aria-labelledby="submit-title">
      <div className="submit-copy">
        <p className="eyebrow">新建采集</p>
        <h2 id="submit-title">提交新的采集任务</h2>
        <p>输入 6 位股票代码，查询到股票名称后才可提交。</p>
      </div>
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
            <strong>{symbolLookup.result.name}（{symbolLookup.result.symbol}）</strong>
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
        <label className="capture-option">
          <input
            type="checkbox"
            aria-label="生成整页长截图"
            checked={includeLongCapture}
            onChange={(event) => setIncludeLongCapture(event.target.checked)}
          />
          <span>
            <strong>生成整页长截图</strong>
            <small>关闭后由 App 在后台请求并回传数据，不打开搜索框、不切换股票页面。</small>
          </span>
        </label>
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
