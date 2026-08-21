import { FormEvent, useEffect, useState } from 'react'
import { AdminPage } from './AdminPage'
import { api, type Capture, type Job, type JobStatus, subscribeToJob } from './api'
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
  const isAdmin = window.location.hash === '#admin'

  useEffect(() => {
    if (!task || initialTask) return
    return subscribeToJob(task.public_id, () => {
      api.getJob(task.public_id).then(setTask).catch(() => undefined)
    })
  }, [task?.public_id, initialTask])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const normalized = symbol.trim()
    if (!/^(?:0|3|6)\d{5}$/.test(normalized)) {
      setError('请输入 6 位沪深股票代码。')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      setTask(await api.submitJob(normalized))
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
      <h1>按股票代码获取三张 Level2 截图</h1>
      <p>提交后进入单设备队列。截图生成后可在 24 小时内查看或下载。</p>
    </header>
    <section className="panel submit-panel">
      <form onSubmit={submit}>
        <label htmlFor="symbol">股票代码</label>
        <div className="form-row">
          <input id="symbol" inputMode="numeric" autoComplete="off" value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="例如 600938" maxLength={6} />
          <button type="submit" disabled={submitting}>{submitting ? '正在提交…' : '提交截图任务'}</button>
        </div>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
    {task && <JobResult task={task} />}
    <footer>仅采集已登录设备中可正常访问的页面，不绕过验证或权限限制。</footer>
  </main>
}
