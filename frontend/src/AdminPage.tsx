import { FormEvent, KeyboardEvent, PointerEvent, useEffect, useRef, useState } from 'react'
import { api, readCsrfToken, type RunnerHealth } from './api'

type DeviceConnection = 'UNCONFIGURED' | 'CONNECTING' | 'ONLINE' | 'OFFLINE'

function DeviceViewport({ locked }: { locked: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const socket = useRef<WebSocket | null>(null)
  const [connection, setConnection] = useState<DeviceConnection>('UNCONFIGURED')
  const streamUrl = import.meta.env.VITE_RUNNER_WS_URL as string | undefined

  useEffect(() => {
    if (!streamUrl) return
    setConnection('CONNECTING')
    try {
      const client = new WebSocket(streamUrl)
      socket.current = client
      client.onopen = () => setConnection('ONLINE')
      client.onclose = () => setConnection('OFFLINE')
      client.onerror = () => setConnection('OFFLINE')
      client.onmessage = (event) => {
        if (typeof event.data !== 'string' || !canvas.current) return
        const image = new Image()
        image.onload = () => {
          const context = canvas.current?.getContext('2d')
          if (context && canvas.current) context.drawImage(image, 0, 0, canvas.current.width, canvas.current.height)
        }
        image.src = event.data
      }
      return () => client.close()
    } catch {
      setConnection('OFFLINE')
    }
  }, [streamUrl])

  function sendInput(payload: object) {
    if (!locked || socket.current?.readyState !== WebSocket.OPEN) return
    socket.current.send(JSON.stringify(payload))
  }

  function onPointer(event: PointerEvent<HTMLCanvasElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    sendInput({ type: 'pointer', action: event.type, x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height })
  }

  function onKey(event: KeyboardEvent<HTMLCanvasElement>) {
    sendInput({ type: 'keyboard', action: event.type, key: event.key })
  }

  const label = connection === 'ONLINE' ? '设备画面已连接（3–5 FPS）' : connection === 'CONNECTING' ? '正在连接设备画面…' : connection === 'UNCONFIGURED' ? '设备画面通道尚未配置，当前离线' : '设备画面连接不可用，当前离线'
  return <section className="device-panel">
    <div className="section-heading"><h2>设备画面</h2><span className={`stream-state stream-${connection.toLowerCase()}`}>{label}</span></div>
    <canvas ref={canvas} width="720" height="480" tabIndex={locked ? 0 : -1} aria-label="远程设备画面" className="device-canvas" onPointerDown={onPointer} onPointerMove={onPointer} onPointerUp={onPointer} onKeyDown={onKey} onKeyUp={onKey} />
    <p className="minor">画面与输入流将在 Runner 接入后启用。仅获得设备控制权的管理员可以发送点击和键盘操作。</p>
  </section>
}

function healthText(health: RunnerHealth | null) {
  return health?.state === 'ONLINE' ? '运行端在线' : '运行端离线'
}

export function AdminPage() {
  const [password, setPassword] = useState('')
  const [authenticated, setAuthenticated] = useState(false)
  const [health, setHealth] = useState<RunnerHealth | null>(null)
  const [locked, setLocked] = useState(false)
  const [message, setMessage] = useState('')
  const [queueNotice, setQueueNotice] = useState('队列控制将在 Runner 协议接入后启用。')

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
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '登录失败')
    }
  }

  async function changeLock(action: 'acquire' | 'release') {
    setMessage('')
    try {
      const csrfToken = readCsrfToken()
      const result = action === 'acquire' ? await api.acquireLock(csrfToken) : await api.releaseLock(csrfToken)
      setLocked(result.locked)
      setMessage(result.locked ? '已获得设备控制权' : '已交还设备控制权')
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '设备锁操作失败')
    }
  }

  useEffect(() => {
    if (!authenticated) return
    const timer = window.setInterval(() => api.runner().then(setHealth).catch(() => setHealth(null)), 15_000)
    return () => window.clearInterval(timer)
  }, [authenticated])

  if (!authenticated) return <main className="admin-shell"><section className="panel admin-login">
    <p className="eyebrow">ADMIN CONSOLE</p><h1>设备管理台</h1>
    <form onSubmit={login}><label htmlFor="password">管理员密码</label><input id="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /><button type="submit">登录管理台</button></form>
    <p className="notice">密码仅用于本次请求，不会记录或展示。</p>{message && <p className="error" role="alert">{message}</p>}
  </section></main>

  return <main className="admin-shell">
    <header className="admin-header"><div><p className="eyebrow">ADMIN CONSOLE</p><h1>设备与队列控制</h1></div><span className={`status ${health?.state === 'ONLINE' ? 'status-completed' : 'status-failed'}`}>{healthText(health)}</span></header>
    <p className="notice"><span>密码仅用于本次请求，不会记录或展示。</span> 不要在此页面输入或粘贴同花顺账号凭据；本页面不会记录任何密码或按键内容。</p>
    <section className="admin-controls"><div><h2>人工接管</h2><p>{locked ? '当前会话正在控制设备。' : '设备未由当前会话接管。'}</p></div><div className="button-row">{locked ? <button className="secondary" onClick={() => changeLock('release')}>交还控制</button> : <button onClick={() => changeLock('acquire')}>接管设备</button>}<button className="secondary" onClick={() => setQueueNotice('恢复自动任务请求已记录，等待 Runner 协议接入。')}>恢复自动任务</button></div></section>
    <section className="admin-controls"><div><h2>队列</h2><p>{queueNotice}</p></div><div className="button-row"><button className="secondary" onClick={() => setQueueNotice('暂停队列功能等待 Runner 协议接入。')}>暂停队列</button><button className="secondary" onClick={() => setQueueNotice('恢复队列功能等待 Runner 协议接入。')}>恢复队列</button></div></section>
    {message && <p className={message.includes('获得') || message.includes('交还') ? 'success-message' : 'error'} role="status">{message}</p>}
    <DeviceViewport locked={locked} />
  </main>
}
