import { FormEvent, useEffect, useState } from 'react'
import { AdminPage } from './AdminPage'
import { api, type Capture, type Job, type JobStatus, type JobStreamState, subscribeToJob } from './api'
import './styles.css'

const captureNames: Record<Capture['kind'], string> = {
  LARGE_ORDER_NET: '大单净额',
  LARGE_ORDER_AMOUNT: '大单金额',
  RETAIL_COUNT: '散户数量',
}

const statusText: Record<JobStatus, string> = {
  QUEUED: '已进入队列',
  RUNNING: '正在采集',
  WAITING_ADMIN: '等待管理员处理',
  COMPLETED: '三张截图已就绪',
  PARTIAL: '部分截图已可用',
  FAILED: '采集未完成',
  EXPIRED: '结果已过期',
}

function CaptureCard({ capture }: { capture: Capture }) {
  const ready = capture.status === 'READY' && capture.url
  return <article className="capture-card">
    <h3>{captureNames[capture.kind]}</h3>
    {ready ? <>
      <p className="ready">截图已就绪</p>
      <p className="minor">{capture.expires_at ? `可用至 ${new Date(capture.expires_at).toLocaleString('zh-CN')}` : '可在有效期内查看'}</p>
      <div className="capture-actions">
        <a href={capture.url ?? undefined} target="_blank" rel="noreferrer">打开截图</a>
        <a href={capture.url ?? undefined} download>下载截图</a>
      </div>
    </> : capture.status === 'EXPIRED' ? <p className="expired">链接已过期</p> : <p className="minor">暂未生成</p>}
  </article>
}

export function JobResult({ task }: { task: Job }) {
  return <section className="job-result" aria-live="polite">
    <div className="job-heading">
      <div>
        <p className="eyebrow">任务 {task.public_id.slice(0, 12)}</p>
        <h2>{task.symbol}</h2>
      </div>
      <span className={`status status-${task.status.toLowerCase()}`}>{statusText[task.status]}</span>
    </div>
    {task.status === 'WAITING_ADMIN' && <p className="notice">设备需要管理员确认，任务会在处理后继续。</p>}
    {task.status === 'FAILED' && <p className="error">{task.error_code ? `错误代码：${task.error_code}` : '请稍后重新提交任务。'}</p>}
    {task.status === 'EXPIRED' && <p className="expired">截图仅保留 24 小时；此任务的下载链接已失效。</p>}
    {task.status !== 'EXPIRED' && <div className="capture-grid">{task.captures.map((capture) => <CaptureCard key={capture.kind} capture={capture} />)}</div>}
  </section>
}

export default function App({ initialTask }: { initialTask?: Job }) {
  const [symbol, setSymbol] = useState('')
  const [task, setTask] = useState<Job | undefined>(initialTask)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [streamState, setStreamState] = useState<JobStreamState | undefined>()
  const isAdmin = window.location.hash === '#admin'

  useEffect(() => {
    if (!task || initialTask) return
    return subscribeToJob(task.public_id, () => {
      api.getJob(task.public_id).then(setTask).catch(() => undefined)
    }, setStreamState)
  }, [task?.public_id, initialTask])

  useEffect(() => {
    if (initialTask) return
    const publicId = new URLSearchParams(window.location.search).get('job')
    if (!publicId) return
    api.getJob(publicId).then(setTask).catch((reason) => setError(reason instanceof Error ? reason.message : '无法恢复任务。'))
  }, [initialTask])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const normalized = symbol.trim().toUpperCase()
    if (!/^[A-Z0-9._-]{1,16}$/.test(normalized)) {
      setError('请输入 1–16 位股票搜索代码（字母、数字、.、_ 或 -）。')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const nextTask = await api.submitJob(normalized)
      window.history.replaceState({}, '', `${window.location.pathname}?job=${encodeURIComponent(nextTask.public_id)}${window.location.hash}`)
      setTask(nextTask)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '提交失败，请稍后重试。')
    } finally {
      setSubmitting(false)
    }
  }

  if (isAdmin) return <AdminPage />

  return <main className="page-shell">
    <header className="hero">
      <p className="eyebrow">THS LEVEL2</p>
      <h1>按股票搜索代码获取三张 Level2 截图</h1>
      <p>提交后进入单设备队列。截图生成后可在 24 小时内查看或下载。</p>
    </header>
    <section className="panel submit-panel">
      <form onSubmit={submit}>
        <label htmlFor="symbol">股票代码</label>
        <div className="form-row">
          <input id="symbol" autoComplete="off" value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="例如 SZ.000001" maxLength={16} />
          <button type="submit" disabled={submitting}>{submitting ? '正在提交…' : '提交截图任务'}</button>
        </div>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
    {task && <JobResult task={task} />}
    {streamState === 'RECONNECTING' && <p className="notice" role="status">任务状态流暂时断开，正在自动重连。</p>}
    <footer>仅采集已登录设备中可正常访问的页面，不绕过验证或权限限制。</footer>
  </main>
}
