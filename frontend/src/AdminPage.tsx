import { FormEvent, KeyboardEvent, PointerEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, readCsrfToken, type AccountSessionStatus, type AdminDeviceHealth, type AdminMacdSettings, type AdminMarketMonitoringItem, type AdminMonitoringStatus, type AdminPushConfig, type DeviceLifecycleAction, type DeviceRole, type MarketAdminUser, type QueueState, type RunnerHealth } from './api'
import { parseDeviceServerMessage, type DeviceInputEvent, type DeviceStatus } from './device-protocol'
import { DeviceInputAdapter } from './device-stream'
import { beijingDateBoundaryToUtc, formatRequestLogTimestamp } from './request-log-time'

type DeviceConnection = 'UNCONFIGURED' | 'CONNECTING' | 'ONLINE' | 'OFFLINE'
type AdminAuthentication = 'AUTHENTICATED' | 'ANONYMOUS'
type AdminTab = 'overview' | 'market' | 'market_list' | 'devices' | 'users' | 'logs'

interface DeviceLifecycleDialogState {
  role: DeviceRole
  action: DeviceLifecycleAction
  title: string
  trigger: HTMLButtonElement | null
}

const actionNames: Record<string,string> = { symbol_search:'股票搜索', symbol_lookup:'股票确认', job_submit:'提交任务', job_retry:'重试任务', market_tab:'行情查询-标签', market_retry:'行情查询-重试', market_query:'行情查询', fund_flow_history:'资金流历史', watchlist_group:'自选分组操作', watchlist_symbol:'自选股操作' }

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
  const startBlocked = !locked || lifecycleBusy || lifecycleUnavailable || (lifecycle.state === 'RUNNING' && deviceHealth?.app === 'ONLINE')
  const shutdownBlocked = !locked || lifecycleBusy || lifecycleUnavailable || lifecycle.state === 'STOPPED'
  const lifecycleError = actionError ?? lifecycle?.error_code ?? null
  const guardLifecycleKey = (event: KeyboardEvent<HTMLButtonElement>, blocked: boolean) => {
    if (blocked && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault()
      event.stopPropagation()
    }
  }
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
        <button
          type="button"
          aria-disabled={startBlocked}
          onKeyDown={(event) => guardLifecycleKey(event, startBlocked)}
          onClick={(event) => {
            if (startBlocked) { event.preventDefault(); event.stopPropagation(); return }
            onLifecycleAction?.(role, 'start_and_launch_app')
          }}
        >启动虚拟机并打开同花顺</button>
        <button
          type="button"
          className="device-danger-button"
          aria-disabled={shutdownBlocked}
          onKeyDown={(event) => guardLifecycleKey(event, shutdownBlocked)}
          onClick={(event) => {
            if (shutdownBlocked) { event.preventDefault(); event.stopPropagation(); return }
            onLifecycleAction?.(role, 'shutdown')
          }}
        >关闭虚拟机</button>
      </div>
      {actionPending && <p className="minor" aria-live="polite">{actionPending === 'shutdown' ? '正在关闭虚拟机…' : '正在启动虚拟机并打开同花顺…'}</p>}
      {lifecycleError && <p className="error" role="alert">{lifecycleError}</p>}
    </div>
    <div className="device-session-controls">
      <div>
        <h3>账号会话</h3>
        <p className="minor" aria-live="polite">账号会话：{accountSessionStateText(sessionStatus)}</p>
        {sessionStatus?.updated_at && <p className="minor">更新时间：{new Date(sessionStatus.updated_at).toLocaleString('zh-CN')}</p>}
        {role === 'main_fund_flow' && sessionStatus?.updated_at && <p className="minor">账户到期日：{sessionStatus.expires_at ?? '暂不可用'}</p>}
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

function isAdminAuthenticationFailure(reason: unknown): boolean {
  return (reason instanceof ApiError && reason.status === 401)
    || (reason instanceof Error && reason.message.includes('authentication'))
}

function MonitoringPushAdmin({ onError, autoLoad = false }: { onError: (reason: unknown) => void, autoLoad?: boolean }) {
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [monitoring, setMonitoring] = useState<AdminMonitoringStatus[]>([])
  const [macd, setMacd] = useState<AdminMacdSettings>()
  const [push, setPush] = useState<AdminPushConfig>()
  const [barkGroups, setBarkGroups] = useState('[]')
  const [rules, setRules] = useState('[]')
  const [premium, setPremium] = useState<import('./market-api').PremiumSnapshot>()

  async function load(silent = false) {
    setBusy(true)
    setStatus('')
    try {
      const [nextMonitoring, nextMacd, nextPush] = await Promise.all([
        api.monitoringStatus(), api.macdSettings(), api.pushConfig(),
      ])
      const nextPremium = await api.premium()
      const normalizedPush: AdminPushConfig = {
        enabled: Boolean(nextPush?.enabled),
        premium_push_enabled: nextPush?.premium_push_enabled !== false,
        bark_groups: Array.isArray(nextPush?.bark_groups) ? nextPush.bark_groups : [],
        sc3_bot: {
          enabled: Boolean(nextPush?.sc3_bot?.enabled),
          base_url: nextPush?.sc3_bot?.base_url ?? 'https://bot-go.apijia.cn',
          token: nextPush?.sc3_bot?.token ?? '',
          token_configured: nextPush?.sc3_bot?.token_configured,
          chat_id: nextPush?.sc3_bot?.chat_id ?? '',
          parse_mode: nextPush?.sc3_bot?.parse_mode ?? 'markdown',
          silent: Boolean(nextPush?.sc3_bot?.silent),
        },
        wecom: {
          enabled: Boolean(nextPush?.wecom?.enabled),
          api_base_url: nextPush?.wecom?.api_base_url ?? 'https://qyapi.weixin.qq.com',
          news_base_url: nextPush?.wecom?.news_base_url ?? '',
          corp_id: nextPush?.wecom?.corp_id ?? '',
          corp_secret: nextPush?.wecom?.corp_secret ?? '',
          corp_secret_configured: nextPush?.wecom?.corp_secret_configured,
          agent_id: Number(nextPush?.wecom?.agent_id ?? 0),
          to_user: nextPush?.wecom?.to_user ?? '@all',
          to_party: nextPush?.wecom?.to_party ?? '',
          to_tag: nextPush?.wecom?.to_tag ?? '',
        },
        rules: Array.isArray(nextPush?.rules) ? nextPush.rules : [],
      }
      setMonitoring(Array.isArray(nextMonitoring) ? nextMonitoring : [])
      setMacd(nextMacd ?? { short: 10, long: 20, signal: 5, marker_threshold: 0.00001 })
      setPush(normalizedPush)
      setPremium(nextPremium)
      setBarkGroups(JSON.stringify(normalizedPush.bark_groups, null, 2))
      setRules(JSON.stringify(normalizedPush.rules, null, 2))
      setLoaded(true)
    } catch (reason) {
      if (!silent) onError(reason)
      else setStatus('监控与推送配置暂不可用，可稍后刷新')
    }
    finally { setBusy(false) }
  }

  useEffect(() => {
    if (autoLoad) void load(true)
  }, [autoLoad])

  async function saveMacd(event: FormEvent) {
    event.preventDefault()
    if (!macd) return
    setBusy(true)
    try { setMacd(await api.saveMacdSettings(macd, readCsrfToken())); setStatus('MACD 参数已保存') }
    catch (reason) { onError(reason) }
    finally { setBusy(false) }
  }

  async function savePush(event: FormEvent) {
    event.preventDefault()
    if (!push) return
    setBusy(true)
    try {
      const next = await api.savePushConfig({
        ...push,
        bark_groups: JSON.parse(barkGroups),
        rules: JSON.parse(rules),
      }, readCsrfToken())
      setPush(next)
      setBarkGroups(JSON.stringify(next.bark_groups, null, 2))
      setRules(JSON.stringify(next.rules, null, 2))
      setStatus('推送渠道与规则已保存')
    } catch (reason) { onError(reason) }
    finally { setBusy(false) }
  }

  if (!loaded) return <section className="admin-controls admin-monitoring-loader"><div><h2>行情监控与推送</h2><p>正在自动加载监控采集、MACD、推送渠道和全局规则配置。</p></div><button type="button" className="secondary" onClick={() => void load()} disabled={busy}>{busy ? '加载中…' : '重新加载配置'}</button></section>
  if (!macd || !push) return null
  return <section className="admin-operations-suite">
    <header><div><p className="eyebrow">MARKET OPERATIONS</p><h2>监控、复盘与推送</h2></div><button type="button" className="secondary" onClick={() => void load()} disabled={busy}>刷新配置</button></header>
    {status && <p className="success-message" role="status">{status}</p>}
    <section className="admin-ops-card"><div className="admin-ops-heading"><h3>监控采集</h3><button type="button" className="secondary" disabled={busy} onClick={async () => { setBusy(true); try { await api.refreshMonitoring(readCsrfToken()); await load(); setStatus('监控数据已刷新') } catch (reason) { onError(reason) } finally { setBusy(false) } }}>立即刷新全部</button></div><div className="admin-monitoring-table">{monitoring.length ? monitoring.map((item) => <div key={item.symbol}><strong>{item.symbol}</strong><span>{item.latest_trade_date ?? '未采集'} {item.latest_time ?? ''}</span><small className={item.last_error ? 'error' : ''}>{item.last_error ?? item.last_sync_at ?? '等待首次采集'}</small></div>) : <p className="minor">当前没有启用监控的用户自选。</p>}</div></section>
    <form className="admin-ops-card" onSubmit={saveMacd}><div className="admin-ops-heading"><h3>MACD 参数</h3><button type="submit" className="secondary" disabled={busy}>保存 MACD 参数</button></div><div className="admin-ops-grid four"><label>MACD 短期<input type="number" min="1" max="199" value={macd.short} onChange={(event) => setMacd({ ...macd, short: Number(event.target.value) })} /></label><label>MACD 长期<input type="number" min="2" max="200" value={macd.long} onChange={(event) => setMacd({ ...macd, long: Number(event.target.value) })} /></label><label>MACD 信号<input type="number" min="1" max="100" value={macd.signal} onChange={(event) => setMacd({ ...macd, signal: Number(event.target.value) })} /></label><label>标记阈值<input type="number" min="0" max="1" step="0.0001" value={macd.marker_threshold} onChange={(event) => setMacd({ ...macd, marker_threshold: Number(event.target.value) })} /></label></div></form>
    <form className="admin-ops-card admin-push-form" onSubmit={savePush}><div className="admin-ops-heading"><h3>推送配置</h3><div className="button-row"><button type="button" className="secondary" disabled={busy} onClick={async () => { try { await api.testPush(readCsrfToken()); setStatus('测试推送已发送') } catch (reason) { onError(reason) } }}>测试推送</button><button type="submit" className="secondary" disabled={busy}>保存推送配置</button></div></div><div className="admin-switch-row"><label><input type="checkbox" checked={push.enabled} onChange={(event) => setPush({ ...push, enabled: event.target.checked })} />启用 Bark 总开关</label><label><input type="checkbox" checked={push.premium_push_enabled} onChange={(event) => setPush({ ...push, premium_push_enabled: event.target.checked })} />启用溢价推送</label></div>
      <div className="admin-channel-grid">
        <section><h3>Bark 多组推送</h3><label>Bark 组 JSON<textarea value={barkGroups} onChange={(event) => setBarkGroups(event.target.value)} rows={10} /></label></section>
        <section><h3>Server酱³</h3><label><input type="checkbox" checked={push.sc3_bot.enabled} onChange={(event) => setPush({ ...push, sc3_bot: { ...push.sc3_bot, enabled: event.target.checked } })} />启用</label><label>API 地址<input value={push.sc3_bot.base_url} onChange={(event) => setPush({ ...push, sc3_bot: { ...push.sc3_bot, base_url: event.target.value } })} /></label><label>Token<input type="password" placeholder={push.sc3_bot.token_configured ? '已配置，留空保持不变' : ''} value={push.sc3_bot.token} onChange={(event) => setPush({ ...push, sc3_bot: { ...push.sc3_bot, token: event.target.value } })} /></label><label>Chat ID<input value={push.sc3_bot.chat_id} onChange={(event) => setPush({ ...push, sc3_bot: { ...push.sc3_bot, chat_id: event.target.value } })} /></label></section>
        <section><h3>企业微信</h3><label><input type="checkbox" checked={push.wecom.enabled} onChange={(event) => setPush({ ...push, wecom: { ...push.wecom, enabled: event.target.checked } })} />启用</label><label>API 地址<input value={push.wecom.api_base_url} onChange={(event) => setPush({ ...push, wecom: { ...push.wecom, api_base_url: event.target.value } })} /></label><label>Corp ID<input value={push.wecom.corp_id} onChange={(event) => setPush({ ...push, wecom: { ...push.wecom, corp_id: event.target.value } })} /></label><label>Corp Secret<input type="password" placeholder={push.wecom.corp_secret_configured ? '已配置，留空保持不变' : ''} value={push.wecom.corp_secret} onChange={(event) => setPush({ ...push, wecom: { ...push.wecom, corp_secret: event.target.value } })} /></label><label>Agent ID<input type="number" value={push.wecom.agent_id} onChange={(event) => setPush({ ...push, wecom: { ...push.wecom, agent_id: Number(event.target.value) } })} /></label><label>接收用户<input value={push.wecom.to_user} onChange={(event) => setPush({ ...push, wecom: { ...push.wecom, to_user: event.target.value } })} /></label></section>
      </div>
      <section className="admin-rules-editor"><h3>推送规则</h3><p className="minor">规则由管理员全局维护，同时用于实时提醒和 market 页复盘。</p><label>规则 JSON<textarea value={rules} onChange={(event) => setRules(event.target.value)} rows={14} /></label></section>
    </form>
    <section className="admin-ops-card"><div className="admin-ops-heading"><div><h3>QDII/LOF 溢价快照</h3><p className="minor">{premium?.as_of ? `最近刷新：${premium.as_of}，有效 ${premium.valid_count} 条` : '尚未刷新'}</p></div><div className="button-row"><button type="button" className="secondary" disabled={busy} onClick={async () => { setBusy(true); try { setPremium(await api.refreshPremium(readCsrfToken())); setStatus('溢价快照已刷新') } catch (reason) { onError(reason) } finally { setBusy(false) } }}>刷新溢价</button><button type="button" className="secondary" disabled={busy || !premium?.valid_count} onClick={async () => { try { await api.pushPremium(readCsrfToken()); setStatus('溢价推送已发送') } catch (reason) { onError(reason) } }}>立即推送溢价</button></div></div></section>
  </section>
}

export function AdminPage({ deviceStreamUrl, autoLoad = false }: { deviceStreamUrl?: string, autoLoad?: boolean }) {
  const [password, setPassword] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newPasswordConfirmation, setNewPasswordConfirmation] = useState('')
  const hadSessionCookie = useRef(Boolean(readCsrfToken()))
  const [authentication, setAuthentication] = useState<AdminAuthentication>(hadSessionCookie.current ? 'AUTHENTICATED' : 'ANONYMOUS')
  const authenticated = authentication === 'AUTHENTICATED'
  const [activeTab, setActiveTab] = useState<AdminTab>('devices')
  const [sessionValidated, setSessionValidated] = useState(false)
  const [health, setHealth] = useState<RunnerHealth | null>(null)
  const [queue, setQueue] = useState<QueueState | null>(null)
  const [locked, setLocked] = useState(false)
  const [waitingTaskId, setWaitingTaskId] = useState('')
  const [failedTaskId, setFailedTaskId] = useState('')
  const [message, setMessage] = useState('')
  const [logs, setLogs] = useState<import('./api').RequestLog[]>([])
  const [logFilter, setLogFilter] = useState('')
  const [logAction, setLogAction] = useState('')
  const [logStatus, setLogStatus] = useState('')
  const [logPage, setLogPage] = useState(0)
  const [logFrom, setLogFrom] = useState('')
  const [logTo, setLogTo] = useState('')
  const [logSymbol, setLogSymbol] = useState('')
  const [logStockName, setLogStockName] = useState('')
  const [logUserName, setLogUserName] = useState('')
  const [logIp, setLogIp] = useState('')
  const [marketUsers, setMarketUsers] = useState<MarketAdminUser[] | null>(null)
  const [marketUsersLoading, setMarketUsersLoading] = useState(false)
  const [marketUsersError, setMarketUsersError] = useState('')
  const [adminMonitoringList, setAdminMonitoringList] = useState<AdminMarketMonitoringItem[]>([])
  const [adminMonitoringLoading, setAdminMonitoringLoading] = useState(false)
  const [adminMonitoringError, setAdminMonitoringError] = useState('')
  const [marketUsername, setMarketUsername] = useState('')
  const [marketTemporaryPassword, setMarketTemporaryPassword] = useState('')
  const [devices, setDevices] = useState<Partial<Record<DeviceRole, AdminDeviceHealth>>>({})
  const [accountSessions, setAccountSessions] = useState<Partial<Record<DeviceRole, AccountSessionStatus>>>({})
  const [sessionRefreshPending, setSessionRefreshPending] = useState<Partial<Record<DeviceRole, boolean>>>({})
  const [sessionErrors, setSessionErrors] = useState<Partial<Record<DeviceRole, string>>>({})
  const [actionPending, setActionPending] = useState<Partial<Record<DeviceRole, DeviceLifecycleAction>>>({})
  const [actionErrors, setActionErrors] = useState<Partial<Record<DeviceRole, string>>>({})
  const [lifecycleDialog, setLifecycleDialog] = useState<DeviceLifecycleDialogState | null>(null)
  const deviceRefreshGeneration = useRef(0)
  const authenticationGeneration = useRef(0)
  const clearAuthenticationState = useCallback(() => {
    authenticationGeneration.current += 1
    deviceRefreshGeneration.current += 1
    setSessionValidated(false)
    setHealth(null)
    setQueue(null)
    setLocked(false)
    setDevices({})
    setAccountSessions({})
    setSessionRefreshPending({})
    setSessionErrors({})
    setActionPending({})
    setActionErrors({})
    setLifecycleDialog(null)
    setMarketUsers(null)
    setActiveTab('devices')
  }, [])
  const invalidateAdminControl = useCallback((reason: unknown) => {
    if (isAdminAuthenticationFailure(reason)) {
      setAuthentication('ANONYMOUS')
      clearAuthenticationState()
      setMessage('管理会话已失效，请重新登录')
      return
    }
    if (reason instanceof ApiError && reason.status === 409) {
      setMessage('设备由其他管理会话控制，请先交还控制')
      return
    }
    setMessage(reason instanceof Error ? reason.message : '管理会话或运行端不可用')
  }, [clearAuthenticationState])
  const refreshDevices = useCallback(async () => {
    const generation = ++deviceRefreshGeneration.current
    const authentication = authenticationGeneration.current
    const nextDevices = await api.devices()
    if (generation !== deviceRefreshGeneration.current || authentication !== authenticationGeneration.current) return
    setDevices(Object.fromEntries(nextDevices.devices.map((device) => [device.role, device])))
  }, [])
  const refreshHealth = useCallback(async () => {
    const authentication = authenticationGeneration.current
    try {
      const [nextHealth, nextQueue] = await Promise.all([api.runner(), api.queue()])
      if (authentication !== authenticationGeneration.current) return
      setHealth(nextHealth)
      setQueue(nextQueue)
      await refreshDevices()
    } catch (reason) {
      if (authentication === authenticationGeneration.current) invalidateAdminControl(reason)
    }
  }, [invalidateAdminControl, refreshDevices])

  const loadDeviceData = useCallback(async () => {
    const authentication = authenticationGeneration.current
    const [deviceResult, sessionResult] = await Promise.allSettled([refreshDevices(), api.accountSessions()])
    if (authentication !== authenticationGeneration.current) return
    const authenticationFailure = [deviceResult, sessionResult]
      .find((result): result is PromiseRejectedResult => result.status === 'rejected' && isAdminAuthenticationFailure(result.reason))
    if (authenticationFailure) {
      invalidateAdminControl(authenticationFailure.reason)
      return
    }
    if (sessionResult.status === 'fulfilled') setAccountSessions(Object.fromEntries(sessionResult.value.sessions.map((status) => [status.role, status])))
    if (deviceResult.status === 'rejected') {
      const message = deviceResult.reason instanceof Error ? deviceResult.reason.message : '设备生命周期状态刷新失败'
      setActionErrors({ core_metrics: message, main_fund_flow: message })
    }
    if (sessionResult.status === 'rejected') {
      const message = sessionResult.reason instanceof Error ? sessionResult.reason.message : '账号会话状态刷新失败'
      setSessionErrors({ core_metrics: message, main_fund_flow: message })
    }
  }, [invalidateAdminControl, refreshDevices])

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setMessage('')
    try {
      await api.login(password)
      setPassword('')
      clearAuthenticationState()
      const authentication = authenticationGeneration.current
      setAuthentication('AUTHENTICATED')
      try {
        const [nextHealth, nextLock, nextQueue] = await Promise.all([api.runner(), api.lock(), api.queue()])
        if (authentication !== authenticationGeneration.current) return
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
      clearAuthenticationState()
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
      clearAuthenticationState()
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
    setMarketUsersLoading(true)
    setMarketUsersError('')
    try { setMarketUsers(await api.marketUsers()) }
    catch (reason) {
      if (isAdminAuthenticationFailure(reason)) invalidateAdminControl(reason)
      else setMarketUsersError(reason instanceof Error ? reason.message : '行情用户加载失败')
    } finally { setMarketUsersLoading(false) }
  }
  async function loadAdminMonitoringList() {
    setAdminMonitoringLoading(true)
    setAdminMonitoringError('')
    try { setAdminMonitoringList(await api.marketMonitoringList()) }
    catch (reason) {
      if (isAdminAuthenticationFailure(reason)) invalidateAdminControl(reason)
      else setAdminMonitoringError(reason instanceof Error ? reason.message : '市场监控列表加载失败')
    } finally { setAdminMonitoringLoading(false) }
  }
  useEffect(() => {
    if (authenticated && activeTab === 'users' && marketUsers === null && !marketUsersLoading) {
      void loadMarketUsers()
    }
  }, [activeTab, authenticated])
  useEffect(() => {
    if (!authenticated || activeTab !== 'market_list') return
    void loadAdminMonitoringList()
  }, [activeTab, authenticated, invalidateAdminControl])
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
    const authentication = authenticationGeneration.current
    setSessionErrors((current) => ({ ...current, [role]: undefined }))
    setSessionRefreshPending((current) => ({ ...current, [role]: true }))
    try {
      const status = await api.refreshAccountSession(role, readCsrfToken())
      if (authentication !== authenticationGeneration.current) return
      setAccountSessions((current) => ({ ...current, [role]: status }))
    } catch (reason) {
      if (authentication !== authenticationGeneration.current) return
      if (isAdminAuthenticationFailure(reason)) {
        invalidateAdminControl(reason)
        return
      }
      try {
        const response = await api.accountSessions()
        if (authentication !== authenticationGeneration.current) return
        const status = response.sessions.find((item) => item.role === role)
        if (status) setAccountSessions((current) => ({ ...current, [role]: status }))
      } catch {
        // Keep the refresh error visible when the status read also fails.
      }
      setSessionErrors((current) => ({ ...current, [role]: reason instanceof Error ? reason.message : '账号会话刷新失败' }))
    } finally {
      if (authentication === authenticationGeneration.current) setSessionRefreshPending((current) => ({ ...current, [role]: false }))
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
    const authentication = authenticationGeneration.current
    setActionPending((current) => ({ ...current, [role]: action }))
    setActionErrors((current) => ({ ...current, [role]: undefined }))
    try {
      const lifecycle = await api.deviceAction(role, action, readCsrfToken())
      if (authentication !== authenticationGeneration.current) return
      deviceRefreshGeneration.current += 1
      setDevices((current) => current[role]
        ? { ...current, [role]: { ...current[role]!, lifecycle } }
        : current)
      if (lifecycle.state === 'ERROR' || lifecycle.error_code) {
        setActionErrors((current) => ({ ...current, [role]: lifecycle.error_code ?? 'DEVICE_LIFECYCLE_FAILED' }))
      }
    } catch (reason) {
      if (authentication !== authenticationGeneration.current) return
      if (isAdminAuthenticationFailure(reason)) {
        invalidateAdminControl(reason)
        return
      }
      setActionErrors((current) => ({ ...current, [role]: reason instanceof Error ? reason.message : 'DEVICE_LIFECYCLE_FAILED' }))
    } finally {
      if (authentication === authenticationGeneration.current) {
        setActionPending((current) => ({ ...current, [role]: undefined }))
        closeLifecycleDialog()
      }
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
        clearAuthenticationState()
        return
      }
      setAuthentication('ANONYMOUS')
      clearAuthenticationState()
      invalidateAdminControl(reason)
    })
    return () => { active = false }
  }, [clearAuthenticationState, invalidateAdminControl])

  useEffect(() => {
    if (!authenticated || !sessionValidated) return
    void loadDeviceData()
  }, [authenticated, loadDeviceData, sessionValidated])

  const lifecycleTransitioning = Object.values(devices).some((device) => device?.lifecycle.state === 'STARTING' || device?.lifecycle.state === 'STOPPING')
  useEffect(() => {
    if (!authenticated || !sessionValidated) return
    let active = true
    let timer: number | undefined
    const delay = lifecycleTransitioning ? 2_000 : 15_000
    const poll = async () => {
      await refreshHealth()
      if (active) timer = window.setTimeout(poll, delay)
    }
    timer = window.setTimeout(poll, delay)
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [authenticated, lifecycleTransitioning, refreshHealth, sessionValidated])

  if (!authenticated) return <main className="admin-shell" data-1p-ignore="true" data-lpignore="true"><section className="panel admin-login"><p className="eyebrow">ADMIN CONSOLE</p><h1>设备管理台</h1><form onSubmit={login} data-1p-ignore="true" data-lpignore="true"><label htmlFor="password">管理员密码</label><input id="password" type="password" autoComplete="current-password" data-1p-ignore="true" data-lpignore="true" value={password} onChange={(event) => setPassword(event.target.value)} required /><button type="submit">登录管理台</button></form><p className="notice">管理员用户名固定为 admin；密码仅用于本次请求，不会记录或展示。</p>{message && <p className="error" role="alert">{message}</p>}</section></main>

  return <main className="admin-shell" data-1p-ignore="true" data-lpignore="true">
    <header className="admin-header"><div><p className="eyebrow">ADMIN CONSOLE</p><h1>设备与队列控制</h1></div><div className="button-row"><span className={`status ${healthReady(health) ? 'status-completed' : health?.state === 'NEEDS_ADMIN' ? 'status-waiting_admin' : 'status-failed'}`}>{healthText(health)}</span><button className="secondary" onClick={logout}>退出管理台</button></div></header>
    <p className="notice"><span>密码仅用于本次请求，不会记录或展示。</span> 不要在此页面输入或粘贴同花顺账号凭据；本页面不会记录任何密码或按键内容。</p>
    <nav className="admin-tabs" aria-label="管理台标签">
      {([['overview', '运行概览'], ['market', '监控与推送'], ['market_list', '市场监控列表'], ['devices', '设备与队列'], ['users', '用户与安全'], ['logs', '查询日志']] as Array<[AdminTab, string]>).map(([tab, label]) => <button key={tab} type="button" className={activeTab === tab ? 'active' : ''} aria-selected={activeTab === tab} onClick={() => setActiveTab(tab)}>{label}</button>)}
    </nav>
    <div className="admin-tab-panel" hidden={activeTab !== 'logs'}>
      <section className="admin-controls"><div><h2>查询日志</h2><p>完整记录访问者、功能、股票、请求路径和结果。</p></div></section>
      <section className="admin-ops-card"><div className="admin-ops-grid four">
        <label>股票代码<input value={logSymbol} onChange={(e) => setLogSymbol(e.target.value)} /></label><label>股票名称<input value={logStockName} onChange={(e) => setLogStockName(e.target.value)} /></label><label>用户账号<input value={logUserName} onChange={(e) => setLogUserName(e.target.value)} /></label><label>IP<input value={logIp} onChange={(e) => setLogIp(e.target.value)} /></label>
        <label>功能<select aria-label="日志功能筛选" value={logAction} onChange={(e) => setLogAction(e.target.value)}><option value="">全部功能</option>{Object.entries(actionNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>开始日期<input type="date" value={logFrom} onChange={(e) => setLogFrom(e.target.value)} /></label><label>结束日期<input type="date" value={logTo} onChange={(e) => setLogTo(e.target.value)} /></label><label>状态<select aria-label="日志状态筛选" value={logStatus} onChange={(e) => setLogStatus(e.target.value)}><option value="">全部状态</option><option value="成功">成功</option><option value="失败">失败</option></select></label>
        <button className="secondary" onClick={() => { const q = new URLSearchParams(); if (logSymbol) q.set('symbol', logSymbol); if (logStockName) q.set('stock_name', logStockName); if (logUserName) q.set('user_name', logUserName); if (logIp) q.set('ip', logIp); if (logAction) q.set('action', logAction); if (logStatus) q.set('status', logStatus); if (logFrom) q.set('from', beijingDateBoundaryToUtc(logFrom, 'start')); if (logTo) q.set('to', beijingDateBoundaryToUtc(logTo, 'end')); q.set('offset', '0'); setLogPage(0); void api.logs('?' + q.toString()).then((r) => setLogs(r.items)).catch(invalidateAdminControl) }}>查询</button>
      </div></section>
      <section className="admin-ops-card" style={{overflowX: 'auto'}}>{logs.length ? <table><thead><tr>{['日期','时间','功能','股票代码','股票名称','用户账号','IP','设备类型','结果','错误码','耗时(ms)','任务ID'].map((h) => <th key={h}>{h}</th>)}</tr></thead><tbody>{logs.map((log) => { const timestamp = formatRequestLogTimestamp(log.timestamp); return <tr key={log.id}><td>{timestamp.date}</td><td>{timestamp.time}</td><td>{actionNames[log.action] ?? '其他功能'}</td><td>{log.symbol ?? '—'}</td><td>{log.stock_name ?? '—'}</td><td>{log.user_name ?? (log.user_id == null ? '匿名' : '未知账号')}</td><td>{log.ip ?? '未知'}</td><td>{log.device_type ?? '其他设备'}</td><td>{log.status_code < 400 && !['FAILED','PARTIAL'].includes(log.task_status ?? '') ? '成功' : '失败'}</td><td>{log.error_code ?? '—'}</td><td>{Math.round(log.duration_ms)}</td><td>{log.public_id ?? '—'}</td></tr> })}</tbody></table> : <p className="minor">暂无查询日志。</p>}</section>
    </div>
    <div className="admin-tab-panel" hidden={activeTab !== 'overview'}>
      <section className="admin-controls"><div><h2>运行概览</h2><p>{healthText(health)} · {queue?.paused ? '队列已暂停' : '队列接收中'} · {locked ? '当前会话已接管设备' : '设备未接管'}</p></div><div className="button-row"><button className="secondary" onClick={refreshHealth}>刷新运行端状态</button></div></section>
      <section className="admin-controls"><div><h2>恢复等待任务</h2><p>完成设备登录、验证或权限处理后，输入任务 ID 重新排入 FIFO 队列。</p></div><form className="button-row" onSubmit={resumeWaitingJob}><label htmlFor="waiting-task">等待任务 ID</label><input id="waiting-task" value={waitingTaskId} onChange={(event) => setWaitingTaskId(event.target.value)} autoComplete="off" /><button className="secondary" type="submit" disabled={!waitingTaskId.trim()}>恢复等待任务</button></form></section>
      <section className="admin-controls"><div><h2>重试失败任务</h2><p>设备恢复后，输入失败任务 ID 重新排入 FIFO 队列；已有合格截图会保留。</p></div><form className="button-row" onSubmit={retryFailedJob}><label htmlFor="failed-task">失败任务 ID</label><input id="failed-task" value={failedTaskId} onChange={(event) => setFailedTaskId(event.target.value)} autoComplete="off" /><button className="secondary" type="submit" disabled={!failedTaskId.trim()}>重试失败任务</button></form></section>
    </div>
    <div className="admin-tab-panel" hidden={activeTab !== 'market'}>
      <MonitoringPushAdmin onError={invalidateAdminControl} autoLoad={autoLoad} />
    </div>
    <div className="admin-tab-panel" hidden={activeTab !== 'market_list'}>
      <section className="admin-controls"><div><h2>市场监控列表</h2><p>列出当前被行情用户纳入监控的股票。</p></div></section>
      <section className="admin-ops-card">
        {adminMonitoringLoading ? <p className="minor" role="status">正在加载市场监控列表…</p>
          : adminMonitoringError ? <div><p className="error" role="alert">{adminMonitoringError}</p><button type="button" className="secondary" onClick={() => void loadAdminMonitoringList()}>重新加载列表</button></div>
            : adminMonitoringList.length === 0 ? <p className="minor">当前没有纳入监控的股票。</p>
              : <table className="admin-market-monitoring-list"><thead><tr>{['股票代码', '股票名称', '纳入监控日期', '监控人员'].map((title) => <th key={title}>{title}</th>)}</tr></thead><tbody>{adminMonitoringList.map((item) => <tr key={item.symbol}><td>{item.symbol}</td><td>{item.stock_name}</td><td>{item.monitoring_date ?? '未知'}</td><td>{item.monitoring_users.join('；')}</td></tr>)}</tbody></table>}
      </section>
    </div>
    <div className="admin-tab-panel" hidden={activeTab !== 'devices'}>
      <section className="admin-controls"><div><h2>人工接管</h2><p>{locked ? '当前会话正在控制设备。' : '设备未由当前会话接管。'}</p></div><div className="button-row">{locked ? <button className="secondary" onClick={() => changeLock('release')}>交还控制</button> : <button onClick={() => changeLock('acquire')}>接管设备</button>}<button className="secondary" onClick={refreshHealth}>刷新运行端状态</button></div></section>
      <section className="admin-controls"><div><h2>队列</h2><p>{queue?.paused ? '队列已暂停；已领取的任务会继续完成。' : '队列正在接收 Runner 的 FIFO 任务。'}</p></div><div className="button-row">{queue?.paused ? <button className="secondary" onClick={() => changeQueue('resume')} disabled={locked}>恢复队列</button> : <button className="secondary" onClick={() => changeQueue('pause')} disabled={!queue}>暂停队列</button>}</div></section>
      <div className="admin-device-grid">
        <DeviceViewport title="八项账号" role="core_metrics" locked={locked} active={authenticated} lifecycle={devices.core_metrics?.lifecycle} actionPending={actionPending.core_metrics ?? null} actionError={actionErrors.core_metrics ?? null} sessionStatus={accountSessions.core_metrics ?? null} sessionRefreshPending={sessionRefreshPending.core_metrics} sessionError={sessionErrors.core_metrics ?? null} onLifecycleAction={openLifecycleDialog} onRefreshSession={refreshAccountSession} streamUrl={deviceStreamUrl ?? roleDeviceStreamUrl('core_metrics')} />
        <DeviceViewport title="资金账号" role="main_fund_flow" warning="当前账号，禁止退出" locked={locked} active={authenticated} lifecycle={devices.main_fund_flow?.lifecycle} actionPending={actionPending.main_fund_flow ?? null} actionError={actionErrors.main_fund_flow ?? null} sessionStatus={accountSessions.main_fund_flow ?? null} sessionRefreshPending={sessionRefreshPending.main_fund_flow} sessionError={sessionErrors.main_fund_flow ?? null} onLifecycleAction={openLifecycleDialog} onRefreshSession={refreshAccountSession} streamUrl={deviceStreamUrl ?? roleDeviceStreamUrl('main_fund_flow')} />
      </div>
    </div>
    <div className="admin-tab-panel" hidden={activeTab !== 'users'}>
      <section className="admin-controls"><div><h2>修改管理员密码</h2><p>修改后当前会话会退出，使用新密码重新登录。</p></div><form className="password-change-form" onSubmit={changePassword} data-1p-ignore="true" data-lpignore="true"><label htmlFor="current-admin-password">当前管理员密码</label><input id="current-admin-password" type="password" autoComplete="current-password" data-1p-ignore="true" data-lpignore="true" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /><label htmlFor="new-admin-password">新管理员密码</label><input id="new-admin-password" type="password" autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required /><label htmlFor="confirm-admin-password">确认新管理员密码</label><input id="confirm-admin-password" type="password" autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" value={newPasswordConfirmation} onChange={(event) => setNewPasswordConfirmation(event.target.value)} required /><button className="secondary" type="submit">修改管理员密码</button></form></section>
      <section className="admin-controls admin-market-users">
      <div><h2>行情用户</h2><p>创建普通用户、自选空间和临时密码。用户首次登录必须修改密码。</p></div>
      <div className="admin-market-user-actions">
        {marketUsers === null ? <div>{marketUsersLoading ? <p className="minor" role="status">正在加载行情用户…</p> : marketUsersError ? <p className="error" role="alert">{marketUsersError}</p> : null}<button type="button" className="secondary" onClick={loadMarketUsers} disabled={marketUsersLoading}>{marketUsersError ? '重新加载行情用户' : '加载行情用户'}</button></div> : <>
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
    </div>
    {message && <p className={message.includes('获得') || message.includes('交还') || message.includes('重新') || message.includes('会话已刷新') ? 'success-message' : 'error'} role="status">{message}</p>}
    {lifecycleDialog && <DeviceLifecycleDialog
      dialog={lifecycleDialog}
      pending={actionPending[lifecycleDialog.role] !== undefined}
      onCancel={closeLifecycleDialog}
      onConfirm={confirmLifecycleAction}
    />}
  </main>
}
