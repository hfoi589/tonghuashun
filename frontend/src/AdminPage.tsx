import { FormEvent, KeyboardEvent, PointerEvent, useCallback, useEffect, useRef, useState } from 'react'
import { api, readCsrfToken, type RunnerHealth } from './api'
import { parseDeviceServerMessage, type DeviceInputEvent } from './device-protocol'
import { DeviceInputAdapter } from './device-stream'

type DeviceConnection = 'UNCONFIGURED' | 'CONNECTING' | 'ONLINE' | 'OFFLINE'

export function DeviceViewport({ locked, active, streamUrl = import.meta.env.VITE_RUNNER_WS_URL as string | undefined }: { locked: boolean; active: boolean; streamUrl?: string }) {
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
    setConnection('CONNECTING')
    const client = new WebSocket(streamUrl)
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
        setRunnerReady(message.state === 'ONLINE')
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
  const onKey = (event: KeyboardEvent<HTMLCanvasElement>) => send({ kind: 'key', key: event.key, action: event.type === 'keydown' ? 'down' : 'up' })
  const label = connection === 'ONLINE' ? '设备画面已连接（3–5 FPS）' : connection === 'CONNECTING' ? '正在连接设备画面…' : connection === 'UNCONFIGURED' ? '设备画面通道尚未配置，当前离线' : '设备画面连接不可用，当前离线'
  return <section className="device-panel">
    <div className="section-heading"><h2>设备画面</h2><span className={`stream-state stream-${connection.toLowerCase()}`}>{label}</span></div>
    <canvas ref={canvas} width="720" height="480" tabIndex={canSend ? 0 : -1} aria-label="远程设备画面" className="device-canvas" onPointerDown={onPointerDown} onPointerUp={onPointerUp} onKeyDown={onKey} onKeyUp={onKey} />
    <p className="minor">Runner 消息使用已定义的 JPEG 帧、状态与输入 envelope；仅当流、Runner 和当前会话锁均就绪时允许输入。</p>
  </section>
}

function healthText(health: RunnerHealth | null) { return health?.state === 'ONLINE' ? '运行端在线' : '运行端离线' }

export function AdminPage({ deviceStreamUrl }: { deviceStreamUrl?: string }) {
  const [password, setPassword] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [health, setHealth] = useState<RunnerHealth | null>(null)
  const [locked, setLocked] = useState(false)
  const [message, setMessage] = useState('')
  const invalidateAdminControl = useCallback((reason: unknown) => {
    setHealth(null)
    setLocked(false)
    setMessage(reason instanceof Error ? reason.message : '管理会话或运行端不可用')
  }, [])
  const refreshHealth = useCallback(async () => {
    try { setHealth(await api.runner()) } catch (reason) { invalidateAdminControl(reason) }
  }, [invalidateAdminControl])

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setMessage('')
    try {
      await api.login(password)
      setPassword('')
      const nextHealth = await api.runner()
      const nextLock = await api.lock()
      setHealth(nextHealth)
      setLocked(nextLock.locked)
      setAuthenticated(true)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '登录失败') }
  }
  async function changeLock(action: 'acquire' | 'release') {
    setMessage('')
    try {
      const result = action === 'acquire' ? await api.acquireLock(readCsrfToken()) : await api.releaseLock(readCsrfToken())
      setLocked(result.locked)
      setMessage(result.locked ? '已获得设备控制权' : '已交还设备控制权')
    } catch (reason) { invalidateAdminControl(reason) }
  }
  useEffect(() => {
    if (!authenticated) return
    const timer = window.setInterval(refreshHealth, 15_000)
    return () => window.clearInterval(timer)
  }, [authenticated, refreshHealth])

  if (!authenticated) return <main className="admin-shell"><section className="panel admin-login"><p className="eyebrow">ADMIN CONSOLE</p><h1>设备管理台</h1><form onSubmit={login}><label htmlFor="password">管理员密码</label><input id="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /><button type="submit">登录管理台</button></form><p className="notice">密码仅用于本次请求，不会记录或展示。</p>{message && <p className="error" role="alert">{message}</p>}</section></main>

  return <main className="admin-shell">
    <header className="admin-header"><div><p className="eyebrow">ADMIN CONSOLE</p><h1>设备与队列控制</h1></div><span className={`status ${health?.state === 'ONLINE' ? 'status-completed' : 'status-failed'}`}>{healthText(health)}</span></header>
    <p className="notice"><span>密码仅用于本次请求，不会记录或展示。</span> 不要在此页面输入或粘贴同花顺账号凭据；本页面不会记录任何密码或按键内容。</p>
    <section className="admin-controls"><div><h2>人工接管</h2><p>{locked ? '当前会话正在控制设备。' : '设备未由当前会话接管。'}</p></div><div className="button-row">{locked ? <button className="secondary" onClick={() => changeLock('release')}>交还控制</button> : <button onClick={() => changeLock('acquire')}>接管设备</button>}<button className="secondary" onClick={refreshHealth}>刷新运行端状态</button></div></section>
    <section className="admin-controls"><div><h2>队列</h2><p>队列控制不可用，等待 Runner 端点接入。</p></div><div className="button-row"><button className="secondary" disabled title="等待 Runner 端点接入">暂停队列</button><button className="secondary" disabled title="等待 Runner 端点接入">恢复队列</button></div></section>
    {message && <p className={message.includes('获得') || message.includes('交还') ? 'success-message' : 'error'} role="status">{message}</p>}
    <DeviceViewport locked={locked} active={health !== null} streamUrl={deviceStreamUrl} />
  </main>
}
