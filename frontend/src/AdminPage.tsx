import { FormEvent, KeyboardEvent, PointerEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, readCsrfToken, type AccountSessionStatus, type AdminDeviceHealth, type DeviceLifecycleAction, type DeviceRole, type MarketAdminUser, type QueueState, type RunnerHealth } from './api'
import { parseDeviceServerMessage, type DeviceInputEvent, type DeviceStatus } from './device-protocol'
import { DeviceInputAdapter } from './device-stream'

type DeviceConnection = 'UNCONFIGURED' | 'CONNECTING' | 'ONLINE' | 'OFFLINE'
type AdminAuthentication = 'AUTHENTICATED' | 'ANONYMOUS'

interface DeviceLifecycleDialogState {
  role: DeviceRole
  action: DeviceLifecycleAction
  title: string
  trigger: HTMLButtonElement | null
}

function defaultDeviceStreamUrl(): string | undefined {
  const configured = import.meta.env.VITE_RUNNER_WS_URL as string | undefined
  if (configured) return configured
  if (typeof window === 'undefined' || !window.location.host) return undefined
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/admin/device`
}

function roleDeviceStreamUrl(role: DeviceRole): string | undefined {
  if (typeof window === 'undefined' || !window.location.host) return undefined
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/admin/devices/${role}`
}

interface DeviceViewportProps {
  locked: boolean
  active: boolean
  streamUrl?: string
  title?: string
  warning?: string
  role: DeviceRole
  lifecycle?: AdminDeviceHealth['lifecycle']
  actionPending?: DeviceLifecycleAction | null
  actionError?: string | null
  sessionStatus?: AccountSessionStatus | null
  sessionRefreshPending?: boolean
  sessionError?: string | null
  onLifecycleAction?: (role: DeviceRole, action: DeviceLifecycleAction) => void
  onRefreshSession?: (role: DeviceRole) => void
}

export function DeviceViewport({
  locked,
  active,
  streamUrl = defaultDeviceStreamUrl(),
  title = '设备画面',
  warning,
  role,
  lifecycle,
  actionPending = null,
  actionError = null,
  sessionStatus = null,
  sessionRefreshPending = false,
  sessionError = null,
  onLifecycleAction,
  onRefreshSession,
}: DeviceViewportProps) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const socket = useRef<WebSocket | null>(null)
  const input = useRef<DeviceInputAdapter | null>(null)
  const pointerStart = useRef<{ x: number; y: number } | null>(null)
  const [connection, setConnection] = useState<DeviceConnection>('UNCONFIGURED')
  const [runnerReady, setRunnerReady] = useState(false)
  const [runnerLocked, setRunnerLocked] = useState(false)
  const [deviceHealth, setDeviceHealth] = useState<DeviceStatus | null>(null)
  if (!input.current) input.current = new DeviceInputAdapter(() => socket.current)

  useEffect(() => {
    if (!active || !streamUrl) {
      socket.current?.close()
      socket.current = null
      setRunnerReady(false)
      setRunnerLocked(false)
      setDeviceHealth(null)
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
      setDeviceHealth(null)
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
      setDeviceHealth(null)
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
      if (message.type === 'device_status') {
        setDeviceHealth(message)
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
  const label = connection === 'ONLINE' ? '设备画面已连接（2 FPS）' : connection === 'CONNECTING' ? '正在连接设备画面…' : connection === 'UNCONFIGURED' ? '设备画面通道尚未配置，当前离线' : '设备画面连接不可用，当前离线'
  const lifecycleBusy = actionPending !== null || lifecycle?.state === 'STARTING' || lifecycle?.state === 'STOPPING'
  const lifecycleUnavailable = !lifecycle || lifecycle.state === 'UNCONFIGURED'
  const startDisabled = !locked || lifecycleBusy || lifecycleUnavailable || (lifecycle.state === 'RUNNING' && deviceHealth?.app === 'ONLINE')
  const shutdownDisabled = !locked || lifecycleBusy || lifecycleUnavailable || lifecycle.state === 'STOPPED'
  return <section className="device-panel">
    <div className="section-heading"><h2>{title}</h2><span className={`stream-state stream-${connection.toLowerCase()}`}>{label}</span></div>
    {warning && <p className="device-account-warning" role="note">{warning}</p>}
    {role === 'main_fund_flow' && <p className="device-protection-note">资金账号受保护：该操作不会切号、清数据、重装 App 或执行页面导航。</p>}
    <dl className="device-health" aria-label={`${title}设备状态`}>
      {(['adb', 'app', 'frida'] as const).map((key) => <div key={key}>
        <dt>{key === 'adb' ? 'ADB' : key === 'app' ? 'App' : 'Frida'}</dt>
        <dd className={`device-health-${(deviceHealth?.[key] ?? 'UNKNOWN').toLowerCase()}`}>
          {deviceHealth?.[key] === 'ONLINE' ? '在线' : deviceHealth?.[key] === 'OFFLINE' ? '离线' : '待检测'}
        </dd>
      </div>)}
    </dl>
    <div className="device-lifecycle-controls">
      <div>
        <h3>虚拟机控制</h3>
        <p className="minor" aria-live="polite">生命周期：{deviceLifecycleStateText(lifecycle)}</p>
        {!locked && <p className="minor">请先接管设备</p>}
      </div>
      <div className="device-lifecycle-actions">
        <button type="button" onClick={() => onLifecycleAction?.(role, 'start_and_launch_app')} disabled={startDisabled}>启动虚拟机并打开同花顺</button>
        <button type="button" className="device-danger-button" onClick={() => onLifecycleAction?.(role, 'shutdown')} disabled={shutdownDisabled}>关闭虚拟机</button>
      </div>
      {actionPending && <p className="minor" aria-live="polite">{actionPending === 'shutdown' ? '正在关闭虚拟机…' : '正在启动虚拟机并打开同花顺…'}</p>}
      {actionError && <p className="error" role="alert">{actionError}</p>}
    </div>
    <div className="device-session-controls">
      <div>
        <h3>账号会话</h3>
        <p className="minor" aria-live="polite">账号会话：{accountSessionStateText(sessionStatus)}</p>
        {sessionStatus?.updated_at && <p className="minor">更新时间：{new Date(sessionStatus.updated_at).toLocaleString('zh-CN')}</p>}
      </div>
      <button className="secondary" type="button" onClick={() => onRefreshSession?.(role)} disabled={sessionRefreshPending}>
        {sessionRefreshPending ? '刷新中…' : `刷新${title}会话`}
      </button>
      {sessionError && <p className="error" role="alert">{sessionError}</p>}
    </div>
    <div className="device-actions" aria-label="设备画面滚动控制">
      <button type="button" className="secondary" onClick={() => scroll('up')} disabled={!canSend}>上翻</button>
      <button type="button" className="secondary" onClick={() => scroll('down')} disabled={!canSend}>下翻</button>
    </div>
    <canvas ref={canvas} width="540" height="960" tabIndex={canSend ? 0 : -1} aria-label={`${title}远程设备画面`} className="device-canvas" onPointerDown={onPointerDown} onPointerUp={onPointerUp} onPointerCancel={onPointerCancel} onKeyDown={onKey} onKeyUp={onKey} />
    <p className="minor">拖动画面或使用“上翻 / 下翻”；点击画面后也可用 ↑/↓、PgUp/PgDn。仅当流、Runner 和当前会话锁均就绪时允许输入。</p>
  </section>
}

function DeviceLifecycleDialog({
  dialog,
  pending,
  onCancel,
  onConfirm,
}: {
  dialog: DeviceLifecycleDialogState
  pending: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const titleId = `device-lifecycle-title-${dialog.role}-${dialog.action}`
  const descriptionId = `device-lifecycle-description-${dialog.role}-${dialog.action}`
  useEffect(() => {
    if (pending) dialogRef.current?.focus()
    else cancelRef.current?.focus()
  }, [pending])
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && !pending) {
      event.preventDefault()
      onCancel()
      return
    }
    if (event.key !== 'Tab') return
    if (pending) {
      event.preventDefault()
      dialogRef.current?.focus()
      return
    }
    const cancel = cancelRef.current
    const confirm = confirmRef.current
    if (!cancel || !confirm) return
    if (event.shiftKey && document.activeElement === cancel) {
      event.preventDefault()
      confirm.focus()
    } else if (!event.shiftKey && document.activeElement === confirm) {
      event.preventDefault()
      cancel.focus()
    }
  }
  const description = dialog.action === 'shutdown'
    ? `关闭“${dialog.title}”虚拟机？这会中断该设备画面以及需要设备的会话刷新或长截图，但不会退出账号、清除数据或影响另一台设备。`
    : `启动“${dialog.title}”虚拟机并打开同花顺？启动完成后只打开同花顺首页；如遇验证码、登录或设备验证，请在设备画面中人工处理。`
  return <div className="device-dialog-backdrop">
    <div
      ref={dialogRef}
      className="device-lifecycle-dialog"
      role="alertdialog"
      tabIndex={-1}
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onKeyDown={onKeyDown}
    >
      <p className="eyebrow">DEVICE LIFECYCLE</p>
      <h2 id={titleId}>{dialog.action === 'shutdown' ? '确认关闭虚拟机' : '确认启动虚拟机'}</h2>
      <p id={descriptionId}>{description}</p>
      {dialog.role === 'main_fund_flow' && <p className="device-protection-note">资金账号受保护：该操作不会切号、清数据、重装 App 或执行页面导航。</p>}
      <div className="device-dialog-actions">
        <button ref={cancelRef} type="button" className="secondary" onClick={onCancel} disabled={pending}>取消</button>
        <button ref={confirmRef} type="button" className={dialog.action === 'shutdown' ? 'device-danger-button' : undefined} onClick={onConfirm} disabled={pending}>
          {pending ? '提交中…' : dialog.action === 'shutdown' ? '确认关闭' : '确认启动'}
        </button>
      </div>
    </div>
  </div>
}

function healthText(health: RunnerHealth | null) {
  if (health?.state === 'READY') return '运行端就绪'
  if (health?.state === 'ADMIN_CONTROL') return '管理员正在控制设备'
  if (health?.state === 'NEEDS_ADMIN') return '需要管理员处理设备'
  if (health?.state === 'BOOTING') return '运行端正在启动'
  return '运行端离线'
}

function healthReady(health: RunnerHealth | null) { return health?.state === 'READY' || health?.state === 'ADMIN_CONTROL' }

function deviceLifecycleStateText(lifecycle: AdminDeviceHealth['lifecycle'] | undefined): string {
  if (!lifecycle) return '待检测'
  return ({
    UNCONFIGURED: '未配置',
    UNKNOWN: '待检测',
    STOPPED: '已关闭',
    STARTING: '启动中',
    RUNNING: '运行中',
    STOPPING: '关闭中',
    ERROR: '异常',
  } as const)[lifecycle.state]
}

function accountSessionStateText(status: AccountSessionStatus | null): string {
  if (!status) return '未查询'
  if (status.state === 'READY') return '已就绪'
  if (status.state === 'MISSING') return '未配置'
  if (status.state === 'ERROR') return status.error_code ? `异常（${status.error_code}）` : '异常'
  return status.state
}

export function AdminPage({ deviceStreamUrl }: { deviceStreamUrl?: string }) {
  const [password, setPassword] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newPasswordConfirmation, setNewPasswordConfirmation] = useState('')
  const hadSessionCookie = useRef(Boolean(readCsrfToken()))
  const [authentication, setAuthentication] = useState<AdminAuthentication>(hadSessionCookie.current ? 'AUTHENTICATED' : 'ANONYMOUS')
  const authenticated = authentication === 'AUTHENTICATED'
  const [sessionValidated, setSessionValidated] = useState(false)
  const [health, setHealth] = useState<RunnerHealth | null>(null)
  const [queue, setQueue] = useState<QueueState | null>(null)
  const [locked, setLocked] = useState(false)
  const [waitingTaskId, setWaitingTaskId] = useState('')
  const [failedTaskId, setFailedTaskId] = useState('')
  const [message, setMessage] = useState('')
  const [marketUsers, setMarketUsers] = useState<MarketAdminUser[] | null>(null)
  const [marketUsername, setMarketUsername] = useState('')
  const [marketTemporaryPassword, setMarketTemporaryPassword] = useState('')
  const [devices, setDevices] = useState<Partial<Record<DeviceRole, AdminDeviceHealth>>>({})
  const [accountSessions, setAccountSessions] = useState<Partial<Record<DeviceRole, AccountSessionStatus>>>({})
  const [sessionRefreshPending, setSessionRefreshPending] = useState<Partial<Record<DeviceRole, boolean>>>({})
  const [sessionErrors, setSessionErrors] = useState<Partial<Record<DeviceRole, string>>>({})
  const [actionPending, setActionPending] = useState<Partial<Record<DeviceRole, DeviceLifecycleAction>>>({})
  const [actionErrors, setActionErrors] = useState<Partial<Record<DeviceRole, string>>>({})
  const [lifecycleDialog, setLifecycleDialog] = useState<DeviceLifecycleDialogState | null>(null)
  const invalidateAdminControl = useCallback((reason: unknown) => {
    const sessionInvalid = (reason instanceof ApiError && reason.status === 401) || (reason instanceof Error && reason.message.includes('authentication'))
    if (sessionInvalid) {
      setAuthentication('ANONYMOUS')
      setSessionValidated(false)
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
      const [nextHealth, nextQueue, nextDevices] = await Promise.all([api.runner(), api.queue(), api.devices()])
      setHealth(nextHealth)
      setQueue(nextQueue)
      setDevices(Object.fromEntries(nextDevices.devices.map((device) => [device.role, device])))
    } catch (reason) { invalidateAdminControl(reason) }
  }, [invalidateAdminControl])

  const loadDeviceData = useCallback(async () => {
    const [deviceResult, sessionResult] = await Promise.allSettled([api.devices(), api.accountSessions()])
    if (deviceResult.status === 'fulfilled') setDevices(Object.fromEntries(deviceResult.value.devices.map((device) => [device.role, device])))
    if (sessionResult.status === 'fulfilled') setAccountSessions(Object.fromEntries(sessionResult.value.sessions.map((status) => [status.role, status])))
  }, [])

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
        setSessionValidated(true)
      } catch (reason) { invalidateAdminControl(reason) }
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '登录失败') }
  }
  async function logout() {
    setMessage('')
    try {
      await api.logout(readCsrfToken())
      setAuthentication('ANONYMOUS')
      setSessionValidated(false)
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
      setSessionValidated(false)
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
  async function loadMarketUsers() {
    setMessage('')
    try { setMarketUsers(await api.marketUsers()) }
    catch (reason) { invalidateAdminControl(reason) }
  }
  async function createMarketUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setMessage('')
    try {
      const created = await api.createMarketUser(marketUsername, marketTemporaryPassword, readCsrfToken())
      setMarketUsers((current) => [...(current ?? []), created])
      setMarketUsername('')
      setMarketTemporaryPassword('')
      setMessage('行情用户已创建')
    } catch (reason) { invalidateAdminControl(reason) }
  }
  async function toggleMarketUser(target: MarketAdminUser) {
    setMessage('')
    try {
      const updated = await api.updateMarketUser(target.id, { enabled: !target.enabled }, readCsrfToken())
      setMarketUsers((current) => current?.map((item) => item.id === updated.id ? updated : item) ?? null)
      setMessage(updated.enabled ? '行情用户已启用' : '行情用户已停用')
    } catch (reason) { invalidateAdminControl(reason) }
  }
  async function refreshAccountSession(role: DeviceRole) {
    setSessionErrors((current) => ({ ...current, [role]: undefined }))
    setSessionRefreshPending((current) => ({ ...current, [role]: true }))
    try {
      const status = await api.refreshAccountSession(role, readCsrfToken())
      setAccountSessions((current) => ({ ...current, [role]: status }))
    } catch (reason) {
      try {
        const response = await api.accountSessions()
        const status = response.sessions.find((item) => item.role === role)
        if (status) setAccountSessions((current) => ({ ...current, [role]: status }))
      } catch {
        // Keep the refresh error visible when the status read also fails.
      }
      setSessionErrors((current) => ({ ...current, [role]: reason instanceof Error ? reason.message : '账号会话刷新失败' }))
    } finally {
      setSessionRefreshPending((current) => ({ ...current, [role]: false }))
    }
  }
  function openLifecycleDialog(role: DeviceRole, action: DeviceLifecycleAction) {
    const title = devices[role]?.label ?? (role === 'core_metrics' ? '八项账号' : '资金账号')
    setActionErrors((current) => ({ ...current, [role]: undefined }))
    setLifecycleDialog({ role, action, title, trigger: document.activeElement instanceof HTMLButtonElement ? document.activeElement : null })
  }
  function closeLifecycleDialog() {
    const trigger = lifecycleDialog?.trigger
    setLifecycleDialog(null)
    window.setTimeout(() => trigger?.focus(), 0)
  }
  async function confirmLifecycleAction() {
    if (!lifecycleDialog) return
    const { role, action } = lifecycleDialog
    setActionPending((current) => ({ ...current, [role]: action }))
    setActionErrors((current) => ({ ...current, [role]: undefined }))
    try {
      const lifecycle = await api.deviceAction(role, action, readCsrfToken())
      setDevices((current) => current[role]
        ? { ...current, [role]: { ...current[role]!, lifecycle } }
        : current)
      if (lifecycle.state === 'ERROR' || lifecycle.error_code) {
        setActionErrors((current) => ({ ...current, [role]: lifecycle.error_code ?? 'DEVICE_LIFECYCLE_FAILED' }))
      }
    } catch (reason) {
      setActionErrors((current) => ({ ...current, [role]: reason instanceof Error ? reason.message : 'DEVICE_LIFECYCLE_FAILED' }))
    } finally {
      setActionPending((current) => ({ ...current, [role]: undefined }))
      closeLifecycleDialog()
    }
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
        setSessionValidated(true)
      })
    }).catch((reason) => {
      if (!active) return
      if (reason instanceof ApiError && reason.status === 401) {
        setAuthentication('ANONYMOUS')
        setSessionValidated(false)
        return
      }
      setAuthentication('ANONYMOUS')
      setSessionValidated(false)
      invalidateAdminControl(reason)
    })
    return () => { active = false }
  }, [invalidateAdminControl])

  useEffect(() => {
    if (!authenticated || !sessionValidated) return
    void loadDeviceData()
  }, [authenticated, loadDeviceData, sessionValidated])

  const lifecycleTransitioning = Object.values(devices).some((device) => device?.lifecycle.state === 'STARTING' || device?.lifecycle.state === 'STOPPING')
  useEffect(() => {
    if (!authenticated || !sessionValidated) return
    const timer = window.setInterval(refreshHealth, lifecycleTransitioning ? 2_000 : 15_000)
    return () => window.clearInterval(timer)
  }, [authenticated, lifecycleTransitioning, refreshHealth, sessionValidated])

  if (!authenticated) return <main className="admin-shell" data-1p-ignore="true" data-lpignore="true"><section className="panel admin-login"><p className="eyebrow">ADMIN CONSOLE</p><h1>设备管理台</h1><form onSubmit={login} data-1p-ignore="true" data-lpignore="true"><label htmlFor="password">管理员密码</label><input id="password" type="password" autoComplete="current-password" data-1p-ignore="true" data-lpignore="true" value={password} onChange={(event) => setPassword(event.target.value)} required /><button type="submit">登录管理台</button></form><p className="notice">管理员用户名固定为 admin；密码仅用于本次请求，不会记录或展示。</p>{message && <p className="error" role="alert">{message}</p>}</section></main>

  return <main className="admin-shell" data-1p-ignore="true" data-lpignore="true">
    <header className="admin-header"><div><p className="eyebrow">ADMIN CONSOLE</p><h1>设备与队列控制</h1></div><div className="button-row"><span className={`status ${healthReady(health) ? 'status-completed' : health?.state === 'NEEDS_ADMIN' ? 'status-waiting_admin' : 'status-failed'}`}>{healthText(health)}</span><button className="secondary" onClick={logout}>退出管理台</button></div></header>
    <p className="notice"><span>密码仅用于本次请求，不会记录或展示。</span> 不要在此页面输入或粘贴同花顺账号凭据；本页面不会记录任何密码或按键内容。</p>
    <section className="admin-controls"><div><h2>人工接管</h2><p>{locked ? '当前会话正在控制设备。' : '设备未由当前会话接管。'}</p></div><div className="button-row">{locked ? <button className="secondary" onClick={() => changeLock('release')}>交还控制</button> : <button onClick={() => changeLock('acquire')}>接管设备</button>}<button className="secondary" onClick={refreshHealth}>刷新运行端状态</button></div></section>
    <section className="admin-controls"><div><h2>队列</h2><p>{queue?.paused ? '队列已暂停；已领取的任务会继续完成。' : '队列正在接收 Runner 的 FIFO 任务。'}</p></div><div className="button-row">{queue?.paused ? <button className="secondary" onClick={() => changeQueue('resume')} disabled={locked}>恢复队列</button> : <button className="secondary" onClick={() => changeQueue('pause')} disabled={!queue}>暂停队列</button>}</div></section>
    <section className="admin-controls"><div><h2>恢复等待任务</h2><p>完成设备登录、验证或权限处理后，输入任务 ID 重新排入 FIFO 队列。</p></div><form className="button-row" onSubmit={resumeWaitingJob}><label htmlFor="waiting-task">等待任务 ID</label><input id="waiting-task" value={waitingTaskId} onChange={(event) => setWaitingTaskId(event.target.value)} autoComplete="off" /><button className="secondary" type="submit" disabled={!waitingTaskId.trim()}>恢复等待任务</button></form></section>
    <section className="admin-controls"><div><h2>重试失败任务</h2><p>设备恢复后，输入失败任务 ID 重新排入 FIFO 队列；已有合格截图会保留。</p></div><form className="button-row" onSubmit={retryFailedJob}><label htmlFor="failed-task">失败任务 ID</label><input id="failed-task" value={failedTaskId} onChange={(event) => setFailedTaskId(event.target.value)} autoComplete="off" /><button className="secondary" type="submit" disabled={!failedTaskId.trim()}>重试失败任务</button></form></section>
    <section className="admin-controls"><div><h2>修改管理员密码</h2><p>修改后当前会话会退出，使用新密码重新登录。</p></div><form className="password-change-form" onSubmit={changePassword} data-1p-ignore="true" data-lpignore="true"><label htmlFor="current-admin-password">当前管理员密码</label><input id="current-admin-password" type="password" autoComplete="current-password" data-1p-ignore="true" data-lpignore="true" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /><label htmlFor="new-admin-password">新管理员密码</label><input id="new-admin-password" type="password" autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required /><label htmlFor="confirm-admin-password">确认新管理员密码</label><input id="confirm-admin-password" type="password" autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" value={newPasswordConfirmation} onChange={(event) => setNewPasswordConfirmation(event.target.value)} required /><button className="secondary" type="submit">修改管理员密码</button></form></section>
    <section className="admin-controls admin-market-users">
      <div><h2>行情用户</h2><p>创建普通用户、自选空间和临时密码。用户首次登录必须修改密码。</p></div>
      <div className="admin-market-user-actions">
        {marketUsers === null ? <button type="button" className="secondary" onClick={loadMarketUsers}>加载行情用户</button> : <>
          <form className="password-change-form" onSubmit={createMarketUser}>
            <label htmlFor="new-market-username">新用户名</label><input id="new-market-username" value={marketUsername} onChange={(event) => setMarketUsername(event.target.value)} minLength={3} required />
            <label htmlFor="new-market-password">临时密码</label><input id="new-market-password" type="password" autoComplete="new-password" value={marketTemporaryPassword} onChange={(event) => setMarketTemporaryPassword(event.target.value)} minLength={8} required />
            <button type="submit" className="secondary">创建行情用户</button>
          </form>
          <div className="admin-market-user-list">{marketUsers.length === 0 ? <p className="minor">还没有行情用户。</p> : marketUsers.map((target) => <div key={target.id}>
            <span><strong>{target.username}</strong><small>{target.must_change_password ? '首次登录需改密' : '密码已设置'}</small></span>
            <button type="button" className="secondary" onClick={() => toggleMarketUser(target)}>{target.enabled ? '停用' : '启用'}</button>
          </div>)}</div>
        </>}
      </div>
    </section>
    {message && <p className={message.includes('获得') || message.includes('交还') || message.includes('重新') || message.includes('会话已刷新') ? 'success-message' : 'error'} role="status">{message}</p>}
    <div className="admin-device-grid">
      <DeviceViewport
        title="八项账号"
        role="core_metrics"
        locked={locked}
        active={authenticated}
        lifecycle={devices.core_metrics?.lifecycle}
        actionPending={actionPending.core_metrics ?? null}
        actionError={actionErrors.core_metrics ?? null}
        sessionStatus={accountSessions.core_metrics ?? null}
        sessionRefreshPending={sessionRefreshPending.core_metrics}
        sessionError={sessionErrors.core_metrics ?? null}
        onLifecycleAction={openLifecycleDialog}
        onRefreshSession={refreshAccountSession}
        streamUrl={deviceStreamUrl ?? roleDeviceStreamUrl('core_metrics')}
      />
      <DeviceViewport
        title="资金账号"
        role="main_fund_flow"
        warning="当前账号，禁止退出"
        locked={locked}
        active={authenticated}
        lifecycle={devices.main_fund_flow?.lifecycle}
        actionPending={actionPending.main_fund_flow ?? null}
        actionError={actionErrors.main_fund_flow ?? null}
        sessionStatus={accountSessions.main_fund_flow ?? null}
        sessionRefreshPending={sessionRefreshPending.main_fund_flow}
        sessionError={sessionErrors.main_fund_flow ?? null}
        onLifecycleAction={openLifecycleDialog}
        onRefreshSession={refreshAccountSession}
        streamUrl={deviceStreamUrl ?? roleDeviceStreamUrl('main_fund_flow')}
      />
    </div>
    {lifecycleDialog && <DeviceLifecycleDialog
      dialog={lifecycleDialog}
      pending={actionPending[lifecycleDialog.role] !== undefined}
      onCancel={closeLifecycleDialog}
      onConfirm={confirmLifecycleAction}
    />}
  </main>
}
