import { FormEvent, KeyboardEvent, PointerEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, readCsrfToken, type QueueState, type RunnerHealth } from './api'
import { parseDeviceServerMessage, type DeviceInputEvent } from './device-protocol'
import { DeviceInputAdapter } from './device-stream'

type DeviceConnection = 'UNCONFIGURED' | 'CONNECTING' | 'ONLINE' | 'OFFLINE'
type AdminAuthentication = 'AUTHENTICATED' | 'ANONYMOUS'

function defaultDeviceStreamUrl(): string | undefined {
  const configured = import.meta.env.VITE_RUNNER_WS_URL as string | undefined
  if (configured) return configured
  if (typeof window === 'undefined' || !window.location.host) return undefined
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/admin/device`
}

export function DeviceViewport({ locked, active, streamUrl = defaultDeviceStreamUrl() }: { locked: boolean; active: boolean; streamUrl?: string }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const socket = useRef<WebSocket | null>(null)
  const input = useRef<DeviceInputAdapter | null>(null)
  const pointerStart = useRef<{ x: number; y: number } | null>(null)
  const [connection, setConnection] = useState<DeviceConnection>('UNCONFIGURED')
  const [runnerReady, setRunnerReady] = useState(false)
  const [runnerLocked, setRunnerLocked] = useState(false)
  if (!input.current) input.current = new DeviceInputAdapter(() => socket.current)

  useEffect(() => {
    if (!active || !streamUrl) {
      socket.current?.close()
      socket.current = null
      setRunnerReady(false)
      setRunnerLocked(false)
      setConnection(active ? 'UNCONFIGURED' : 'OFFLINE')
      return
    }
    let client: WebSocket
    try {
      const url = new URL(streamUrl)
      if (url.protocol !== 'ws:' && url.protocol !== 'wss:') throw new TypeError('Unsupported WebSocket protocol')
      setConnection('CONNECTING')
      client = new WebSocket(streamUrl)
    } catch {
      socket.current = null
      setRunnerReady(false)
      setRunnerLocked(false)
      setConnection('OFFLINE')
      return
    }
    socket.current = client
    client.onopen = () => setConnection('ONLINE')
    client.onclose = () => {
      if (socket.current === client) socket.current = null
      setConnection('OFFLINE')
      setRunnerReady(false)
      setRunnerLocked(false)
    }
    client.onerror = () => setConnection('OFFLINE')
    client.onmessage = (event) => {
      if (typeof event.data !== 'string') return
      let decoded: unknown
      try { decoded = JSON.parse(event.data) } catch { return }
      const message = parseDeviceServerMessage(decoded)
      if (!message) return
      if (message.type === 'runner_status') {
        setRunnerReady(message.state === 'READY' || message.state === 'ADMIN_CONTROL')
        setRunnerLocked(message.locked)
        return
      }
      const image = new Image()
      image.onload = () => {
        const context = canvas.current?.getContext('2d')
        if (context && canvas.current) context.drawImage(image, 0, 0, canvas.current.width, canvas.current.height)
      }
      image.src = `data:image/jpeg;base64,${message.data}`
    }
    return () => client.close()
  }, [active, streamUrl])

  const canSend = active && connection === 'ONLINE' && runnerReady && locked && runnerLocked
  const position = (event: PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    return { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height }
  }
  const send = (event: DeviceInputEvent) => input.current!.send({ ready: canSend, locked: locked && runnerLocked }, event)
  const onPointerDown = (event: PointerEvent<HTMLCanvasElement>) => { pointerStart.current = position(event) }
  const onPointerUp = (event: PointerEvent<HTMLCanvasElement>) => {
    const start = pointerStart.current
    const end = position(event)
    pointerStart.current = null
    if (!start) return
    if (Math.abs(start.x - end.x) < 0.01 && Math.abs(start.y - end.y) < 0.01) send({ kind: 'tap', ...end })
    else send({ kind: 'swipe', startX: start.x, startY: start.y, endX: end.x, endY: end.y })
  }
  const onPointerCancel = () => { pointerStart.current = null }
  const onKey = (event: KeyboardEvent<HTMLCanvasElement>) => send({ kind: 'key', key: event.key, action: event.type === 'keydown' ? 'down' : 'up' })
  const scroll = (direction: 'up' | 'down') => send({
    kind: 'swipe',
    startX: 0.5,
    startY: direction === 'up' ? 0.3 : 0.72,
    endX: 0.5,
    endY: direction === 'up' ? 0.72 : 0.3,
  })
  const label = connection === 'ONLINE' ? '设备画面已连接（3–5 FPS）' : connection === 'CONNECTING' ? '正在连接设备画面…' : connection === 'UNCONFIGURED' ? '设备画面通道尚未配置，当前离线' : '设备画面连接不可用，当前离线'
  return <section className="device-panel">
    <div className="section-heading"><h2>设备画面</h2><span className={`stream-state stream-${connection.toLowerCase()}`}>{label}</span></div>
    <div className="device-actions" aria-label="设备画面滚动控制">
      <button type="button" className="secondary" onClick={() => scroll('up')} disabled={!canSend}>上翻</button>
      <button type="button" className="secondary" onClick={() => scroll('down')} disabled={!canSend}>下翻</button>
    </div>
    <canvas ref={canvas} width="540" height="960" tabIndex={canSend ? 0 : -1} aria-label="远程设备画面" className="device-canvas" onPointerDown={onPointerDown} onPointerUp={onPointerUp} onPointerCancel={onPointerCancel} onKeyDown={onKey} onKeyUp={onKey} />
    <p className="minor">拖动画面或使用“上翻 / 下翻”；点击画面后也可用 ↑/↓、PgUp/PgDn。仅当流、Runner 和当前会话锁均就绪时允许输入。</p>
  </section>
}

function healthText(health: RunnerHealth | null) {
  if (health?.state === 'READY') return '运行端就绪'
  if (health?.state === 'ADMIN_CONTROL') return '管理员正在控制设备'
  if (health?.state === 'NEEDS_ADMIN') return '需要管理员处理设备'
  if (health?.state === 'BOOTING') return '运行端正在启动'
  return '运行端离线'
}

function healthReady(health: RunnerHealth | null) { return health?.state === 'READY' || health?.state === 'ADMIN_CONTROL' }

export function AdminPage({ deviceStreamUrl }: { deviceStreamUrl?: string }) {
  const [password, setPassword] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newPasswordConfirmation, setNewPasswordConfirmation] = useState('')
  const hadSessionCookie = useRef(Boolean(readCsrfToken()))
  const [authentication, setAuthentication] = useState<AdminAuthentication>(hadSessionCookie.current ? 'AUTHENTICATED' : 'ANONYMOUS')
  const authenticated = authentication === 'AUTHENTICATED'
  const [health, setHealth] = useState<RunnerHealth | null>(null)
  const [queue, setQueue] = useState<QueueState | null>(null)
  const [locked, setLocked] = useState(false)
  const [waitingTaskId, setWaitingTaskId] = useState('')
  const [failedTaskId, setFailedTaskId] = useState('')
  const [message, setMessage] = useState('')
  const invalidateAdminControl = useCallback((reason: unknown) => {
    const sessionInvalid = (reason instanceof ApiError && reason.status === 401) || (reason instanceof Error && reason.message.includes('authentication'))
    if (sessionInvalid) {
      setAuthentication('ANONYMOUS')
      setHealth(null)
      setQueue(null)
      setLocked(false)
      setMessage('管理会话已失效，请重新登录')
      return
    }
    if (reason instanceof ApiError && reason.status === 409) {
      setMessage('设备由其他管理会话控制，请先交还控制')
      return
    }
    setMessage(reason instanceof Error ? reason.message : '管理会话或运行端不可用')
  }, [])
  const refreshHealth = useCallback(async () => {
    try {
      const [nextHealth, nextQueue] = await Promise.all([api.runner(), api.queue()])
      setHealth(nextHealth)
      setQueue(nextQueue)
    } catch (reason) { invalidateAdminControl(reason) }
  }, [invalidateAdminControl])

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setMessage('')
    try {
      await api.login(password)
      setPassword('')
      setAuthentication('AUTHENTICATED')
      try {
        const [nextHealth, nextLock, nextQueue] = await Promise.all([api.runner(), api.lock(), api.queue()])
        setHealth(nextHealth)
        setLocked(nextLock.locked)
        setQueue(nextQueue)
      } catch (reason) { invalidateAdminControl(reason) }
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '登录失败') }
  }
  async function logout() {
    setMessage('')
    try {
      await api.logout(readCsrfToken())
      setAuthentication('ANONYMOUS')
      setHealth(null)
      setQueue(null)
      setLocked(false)
      setMessage('已退出管理台')
    } catch (reason) { invalidateAdminControl(reason) }
  }
  async function changeLock(action: 'acquire' | 'release') {
    setMessage('')
    try {
      const result = action === 'acquire' ? await api.acquireLock(readCsrfToken()) : await api.releaseLock(readCsrfToken())
      setLocked(result.locked)
      if (result.locked) setQueue((current) => current ? { ...current, paused: true } : current)
      setMessage(result.locked ? '已获得设备控制权' : '已交还设备控制权')
    } catch (reason) { invalidateAdminControl(reason) }
  }
  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setMessage('')
    try {
      await api.changePassword(currentPassword, newPassword, newPasswordConfirmation, readCsrfToken())
      setCurrentPassword('')
      setNewPassword('')
      setNewPasswordConfirmation('')
      setAuthentication('ANONYMOUS')
      setHealth(null)
      setQueue(null)
      setLocked(false)
      setMessage('密码已修改，请使用新密码重新登录')
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '密码修改失败') }
  }
  async function changeQueue(action: 'pause' | 'resume') {
    setMessage('')
    try {
      const result = action === 'pause' ? await api.pauseQueue(readCsrfToken()) : await api.resumeQueue(readCsrfToken())
      setQueue(result)
      setMessage(result.paused ? '队列已暂停' : '队列已恢复')
    } catch (reason) { invalidateAdminControl(reason) }
  }
  async function resumeWaitingJob(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const publicId = waitingTaskId.trim()
    if (!publicId) return
    setMessage('')
    try {
      await api.resumeWaitingJob(publicId, readCsrfToken())
      setWaitingTaskId('')
      setMessage('任务已重新加入队列')
    } catch (reason) { invalidateAdminControl(reason) }
  }
  async function retryFailedJob(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const publicId = failedTaskId.trim()
    if (!publicId) return
    setMessage('')
    try {
      await api.retryFailedJob(publicId, readCsrfToken())
      setFailedTaskId('')
      setMessage('失败任务已重新加入队列')
    } catch (reason) { invalidateAdminControl(reason) }
  }
  useEffect(() => {
    if (!hadSessionCookie.current) return
    let active = true
    void api.adminSession().then(() => {
      if (!active) return
      setAuthentication('AUTHENTICATED')
      return Promise.all([api.runner(), api.lock(), api.queue()]).then(([nextHealth, nextLock, nextQueue]) => {
        if (!active) return
        setHealth(nextHealth)
        setLocked(nextLock.locked)
        setQueue(nextQueue)
      })
    }).catch((reason) => {
      if (!active) return
      if (reason instanceof ApiError && reason.status === 401) {
        setAuthentication('ANONYMOUS')
        return
      }
      setAuthentication('ANONYMOUS')
      invalidateAdminControl(reason)
    })
    return () => { active = false }
  }, [invalidateAdminControl])

  useEffect(() => {
    if (!authenticated) return
    const timer = window.setInterval(refreshHealth, 15_000)
    return () => window.clearInterval(timer)
  }, [authenticated, invalidateAdminControl, refreshHealth])

  if (!authenticated) return <main className="admin-shell" data-1p-ignore="true" data-lpignore="true"><section className="panel admin-login"><p className="eyebrow">ADMIN CONSOLE</p><h1>设备管理台</h1><form onSubmit={login} data-1p-ignore="true" data-lpignore="true"><label htmlFor="password">管理员密码</label><input id="password" type="password" autoComplete="current-password" data-1p-ignore="true" data-lpignore="true" value={password} onChange={(event) => setPassword(event.target.value)} required /><button type="submit">登录管理台</button></form><p className="notice">管理员用户名固定为 admin；密码仅用于本次请求，不会记录或展示。</p>{message && <p className="error" role="alert">{message}</p>}</section></main>

  return <main className="admin-shell" data-1p-ignore="true" data-lpignore="true">
    <header className="admin-header"><div><p className="eyebrow">ADMIN CONSOLE</p><h1>设备与队列控制</h1></div><div className="button-row"><span className={`status ${healthReady(health) ? 'status-completed' : health?.state === 'NEEDS_ADMIN' ? 'status-waiting_admin' : 'status-failed'}`}>{healthText(health)}</span><button className="secondary" onClick={logout}>退出管理台</button></div></header>
    <p className="notice"><span>密码仅用于本次请求，不会记录或展示。</span> 不要在此页面输入或粘贴同花顺账号凭据；本页面不会记录任何密码或按键内容。</p>
    <section className="admin-controls"><div><h2>人工接管</h2><p>{locked ? '当前会话正在控制设备。' : '设备未由当前会话接管。'}</p></div><div className="button-row">{locked ? <button className="secondary" onClick={() => changeLock('release')}>交还控制</button> : <button onClick={() => changeLock('acquire')}>接管设备</button>}<button className="secondary" onClick={refreshHealth}>刷新运行端状态</button></div></section>
    <section className="admin-controls"><div><h2>队列</h2><p>{queue?.paused ? '队列已暂停；已领取的任务会继续完成。' : '队列正在接收 Runner 的 FIFO 任务。'}</p></div><div className="button-row">{queue?.paused ? <button className="secondary" onClick={() => changeQueue('resume')} disabled={locked}>恢复队列</button> : <button className="secondary" onClick={() => changeQueue('pause')} disabled={!queue}>暂停队列</button>}</div></section>
    <section className="admin-controls"><div><h2>恢复等待任务</h2><p>完成设备登录、验证或权限处理后，输入任务 ID 重新排入 FIFO 队列。</p></div><form className="button-row" onSubmit={resumeWaitingJob}><label htmlFor="waiting-task">等待任务 ID</label><input id="waiting-task" value={waitingTaskId} onChange={(event) => setWaitingTaskId(event.target.value)} autoComplete="off" /><button className="secondary" type="submit" disabled={!waitingTaskId.trim()}>恢复等待任务</button></form></section>
    <section className="admin-controls"><div><h2>重试失败任务</h2><p>设备恢复后，输入失败任务 ID 重新排入 FIFO 队列；已有合格截图会保留。</p></div><form className="button-row" onSubmit={retryFailedJob}><label htmlFor="failed-task">失败任务 ID</label><input id="failed-task" value={failedTaskId} onChange={(event) => setFailedTaskId(event.target.value)} autoComplete="off" /><button className="secondary" type="submit" disabled={!failedTaskId.trim()}>重试失败任务</button></form></section>
    <section className="admin-controls"><div><h2>修改管理员密码</h2><p>修改后当前会话会退出，使用新密码重新登录。</p></div><form className="password-change-form" onSubmit={changePassword} data-1p-ignore="true" data-lpignore="true"><label htmlFor="current-admin-password">当前管理员密码</label><input id="current-admin-password" type="password" autoComplete="current-password" data-1p-ignore="true" data-lpignore="true" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /><label htmlFor="new-admin-password">新管理员密码</label><input id="new-admin-password" type="password" autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required /><label htmlFor="confirm-admin-password">确认新管理员密码</label><input id="confirm-admin-password" type="password" autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" value={newPasswordConfirmation} onChange={(event) => setNewPasswordConfirmation(event.target.value)} required /><button className="secondary" type="submit">修改管理员密码</button></form></section>
    {message && <p className={message.includes('获得') || message.includes('交还') || message.includes('重新') ? 'success-message' : 'error'} role="status">{message}</p>}
    <DeviceViewport locked={locked} active={authenticated} streamUrl={deviceStreamUrl} />
  </main>
}
