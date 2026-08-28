import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AdminPage, DeviceViewport } from './AdminPage'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderLoggedOutAdmin(deviceStreamUrl?: string) {
  document.cookie = ''
  const view = render(<AdminPage deviceStreamUrl={deviceStreamUrl} />)
  document.cookie = 'ths_csrf=test-csrf'
  return view
}

function mockAuthenticatedDashboard(options: {
  locked?: boolean
  coreState?: string
  coreApp?: string
  fundState?: string
  fundApp?: string
} = {}) {
  const {
    locked = true,
    coreState = 'RUNNING',
    coreApp = 'ONLINE',
    fundState = 'STOPPED',
    fundApp = 'OFFLINE',
  } = options
  vi.mocked(fetch).mockImplementation(async (input) => {
    const url = String(input)
    if (url === '/api/admin/session') return new Response(null, { status: 204 })
    if (url === '/api/admin/runner') return jsonResponse({ state: 'ADMIN_CONTROL', last_heartbeat: null, queue_paused: true })
    if (url === '/api/admin/lock') return jsonResponse({ locked })
    if (url === '/api/admin/queue') return jsonResponse({ paused: true })
    if (url === '/api/admin/devices') return jsonResponse({ devices: [
      {
        role: 'core_metrics', label: '八项账号', adb: 'ONLINE', app: coreApp, frida: 'ONLINE',
        lifecycle: { state: coreState, operation_id: null, error_code: null, updated_at: null },
      },
      {
        role: 'main_fund_flow', label: '资金账号', adb: 'OFFLINE', app: fundApp, frida: 'OFFLINE',
        lifecycle: { state: fundState, operation_id: null, error_code: null, updated_at: null },
      },
    ] })
    if (url === '/api/admin/account-sessions') return jsonResponse({ sessions: [
      { role: 'core_metrics', state: 'READY', updated_at: null, error_code: null },
      { role: 'main_fund_flow', state: 'READY', updated_at: null, error_code: null },
    ] })
    throw new Error(`unexpected request: ${url}`)
  })
}

describe('AdminPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input) => {
      const url = String(input)
      if (url === '/api/admin/devices') return jsonResponse({ devices: [] })
      if (url === '/api/admin/account-sessions') return jsonResponse({ sessions: [] })
      throw new Error(`unexpected request: ${url}`)
    }))
    Object.defineProperty(document, 'cookie', { writable: true, value: 'ths_csrf=test-csrf' })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('loads per-device lifecycle and account-session controls into both device cards', async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input)
      if (url === '/api/admin/session') return new Response(null, { status: 204 })
      if (url === '/api/admin/runner') return jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false })
      if (url === '/api/admin/lock') return jsonResponse({ locked: false })
      if (url === '/api/admin/queue') return jsonResponse({ paused: false })
      if (url === '/api/admin/devices') return jsonResponse({
        devices: [
          {
            role: 'core_metrics', label: '八项账号', adb: 'ONLINE', app: 'ONLINE', frida: 'ONLINE',
            lifecycle: { state: 'RUNNING', operation_id: null, error_code: null, updated_at: '2026-08-27T10:00:00+00:00' },
          },
          {
            role: 'main_fund_flow', label: '资金账号', adb: 'OFFLINE', app: 'OFFLINE', frida: 'OFFLINE',
            lifecycle: { state: 'STOPPED', operation_id: null, error_code: null, updated_at: '2026-08-27T10:00:00+00:00' },
          },
        ],
      })
      if (url === '/api/admin/account-sessions') return jsonResponse({ sessions: [
        { role: 'core_metrics', state: 'READY', updated_at: '2026-08-27T10:00:00+00:00', error_code: null },
        { role: 'main_fund_flow', state: 'MISSING', updated_at: null, error_code: null },
      ] })
      throw new Error(`unexpected request: ${url}`)
    })

    render(<AdminPage />)

    const corePanel = (await screen.findByRole('heading', { name: '八项账号' })).closest('section')!
    const fundPanel = screen.getByRole('heading', { name: '资金账号' }).closest('section')!
    await waitFor(() => expect(within(corePanel).getByText('生命周期：运行中')).toBeInTheDocument())
    expect(within(fundPanel).getByText('生命周期：已关闭')).toBeInTheDocument()
    for (const panel of [corePanel, fundPanel]) {
      expect(within(panel).getByRole('button', { name: '启动虚拟机并打开同花顺' })).toHaveAttribute('aria-disabled', 'true')
      expect(within(panel).getByRole('button', { name: '关闭虚拟机' })).toHaveAttribute('aria-disabled', 'true')
    }
    expect(within(corePanel).getByText('账号会话：已就绪')).toBeInTheDocument()
    expect(within(corePanel).getByText(/更新时间：/)).toBeInTheDocument()
    expect(within(corePanel).getByRole('button', { name: '刷新八项账号会话' })).toBeInTheDocument()
    expect(within(fundPanel).getByText('账号会话：未配置')).toBeInTheDocument()
    expect(within(fundPanel).getByRole('button', { name: '刷新资金账号会话' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '资金账号会话' })).not.toBeInTheDocument()
  })

  it('uses an alertdialog with trapped focus and restores the shutdown trigger on cancel or Escape', async () => {
    mockAuthenticatedDashboard()
    const user = userEvent.setup()
    render(<AdminPage />)
    const corePanel = (await screen.findByRole('heading', { name: '八项账号' })).closest('section')!
    const shutdown = within(corePanel).getByRole('button', { name: '关闭虚拟机' })
    await waitFor(() => expect(shutdown).toBeEnabled())

    await user.click(shutdown)
    const dialog = screen.getByRole('alertdialog', { name: '确认关闭虚拟机' })
    const cancel = within(dialog).getByRole('button', { name: '取消' })
    const confirm = within(dialog).getByRole('button', { name: '确认关闭' })
    expect(cancel).toHaveFocus()
    await user.tab({ shift: true })
    expect(confirm).toHaveFocus()
    await user.tab()
    expect(cancel).toHaveFocus()
    await user.click(cancel)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() => expect(shutdown).toHaveFocus())

    await user.click(shutdown)
    expect(within(screen.getByRole('alertdialog')).getByRole('button', { name: '取消' })).toHaveFocus()
    fireEvent.keyDown(screen.getByRole('alertdialog'), { key: 'Escape' })
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() => expect(shutdown).toHaveFocus())
    expect(vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes('/actions'))).toHaveLength(0)
  })

  it('routes fixed lifecycle actions with CSRF and keeps pending and errors inside the selected card', async () => {
    let resolveAction!: (value: Response) => void
    const actionResponse = new Promise<Response>((resolve) => { resolveAction = resolve })
    mockAuthenticatedDashboard()
    const baseImplementation = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation((input, init) => {
      const url = String(input)
      if (url === '/api/admin/devices/core_metrics/actions') return actionResponse
      return baseImplementation(input, init)
    })
    const user = userEvent.setup()
    render(<AdminPage />)
    const corePanel = (await screen.findByRole('heading', { name: '八项账号' })).closest('section')!
    const fundPanel = screen.getByRole('heading', { name: '资金账号' }).closest('section')!
    const shutdown = within(corePanel).getByRole('button', { name: '关闭虚拟机' })
    await waitFor(() => expect(shutdown).toBeEnabled())
    await user.click(shutdown)
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '确认关闭' }))

    const pendingDialog = screen.getByRole('alertdialog')
    expect(within(pendingDialog).getByRole('button', { name: '提交中…' })).toBeDisabled()
    await waitFor(() => expect(pendingDialog).toHaveFocus())
    expect(within(corePanel).getByText('正在关闭虚拟机…')).toBeInTheDocument()
    expect(within(corePanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })).toHaveAttribute('aria-disabled', 'true')
    expect(within(fundPanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })).toHaveAttribute('aria-disabled', 'false')
    expect(fetch).toHaveBeenCalledWith('/api/admin/devices/core_metrics/actions', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
      body: JSON.stringify({ action: 'shutdown' }),
    }))

    resolveAction(jsonResponse({ state: 'ERROR', operation_id: 'op-core', error_code: 'DEVICE_SHUTDOWN_FAILED', updated_at: null }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(within(corePanel).getByRole('alert')).toHaveTextContent('DEVICE_SHUTDOWN_FAILED')
    expect(within(fundPanel).queryByRole('alert')).not.toBeInTheDocument()
    await waitFor(() => expect(shutdown).toHaveFocus())
  })

  it('surfaces a lifecycle error returned by a later device refresh in only the matching card', async () => {
    let deviceReads = 0
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input)
      if (url === '/api/admin/session') return new Response(null, { status: 204 })
      if (url === '/api/admin/runner') return jsonResponse({ state: 'ADMIN_CONTROL', last_heartbeat: null, queue_paused: true })
      if (url === '/api/admin/lock') return jsonResponse({ locked: true })
      if (url === '/api/admin/queue') return jsonResponse({ paused: true })
      if (url === '/api/admin/account-sessions') return jsonResponse({ sessions: [] })
      if (url === '/api/admin/devices/core_metrics/actions') return jsonResponse({ state: 'STARTING', operation_id: 'op-core', error_code: null, updated_at: null }, 202)
      if (url === '/api/admin/devices') {
        deviceReads += 1
        const failed = deviceReads > 1
        return jsonResponse({ devices: [
          {
            role: 'core_metrics', label: '八项账号', adb: 'ONLINE', app: 'OFFLINE', frida: 'ONLINE',
            lifecycle: failed
              ? { state: 'ERROR', operation_id: 'op-core', error_code: 'DEVICE_BOOT_TIMEOUT', updated_at: null }
              : { state: 'STOPPED', operation_id: null, error_code: null, updated_at: null },
          },
          {
            role: 'main_fund_flow', label: '资金账号', adb: 'ONLINE', app: 'ONLINE', frida: 'ONLINE',
            lifecycle: { state: 'RUNNING', operation_id: null, error_code: null, updated_at: null },
          },
        ] })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    const user = userEvent.setup()
    render(<AdminPage />)
    const corePanel = (await screen.findByRole('heading', { name: '八项账号' })).closest('section')!
    const fundPanel = screen.getByRole('heading', { name: '资金账号' }).closest('section')!
    const start = within(corePanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })
    await waitFor(() => expect(start).toBeEnabled())
    await user.click(start)
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '确认启动' }))
    await waitFor(() => expect(within(corePanel).getByText('生命周期：启动中')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: '刷新运行端状态' }))

    await waitFor(() => expect(within(corePanel).getByRole('alert')).toHaveTextContent('DEVICE_BOOT_TIMEOUT'))
    expect(within(fundPanel).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('ignores a stale device refresh that completes after a lifecycle action starts', async () => {
    let resolveStaleRefresh!: (value: Response) => void
    const staleRefresh = new Promise<Response>((resolve) => { resolveStaleRefresh = resolve })
    let deviceReads = 0
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input)
      if (url === '/api/admin/session') return new Response(null, { status: 204 })
      if (url === '/api/admin/runner') return jsonResponse({ state: 'ADMIN_CONTROL', last_heartbeat: null, queue_paused: true })
      if (url === '/api/admin/lock') return jsonResponse({ locked: true })
      if (url === '/api/admin/queue') return jsonResponse({ paused: true })
      if (url === '/api/admin/account-sessions') return jsonResponse({ sessions: [] })
      if (url === '/api/admin/devices/core_metrics/actions') return jsonResponse({ state: 'STARTING', operation_id: 'op-core', error_code: null, updated_at: null }, 202)
      if (url === '/api/admin/devices') {
        deviceReads += 1
        if (deviceReads === 2) return staleRefresh
        return jsonResponse({ devices: [
          {
            role: 'core_metrics', label: '八项账号', adb: 'ONLINE', app: 'OFFLINE', frida: 'ONLINE',
            lifecycle: { state: 'STOPPED', operation_id: null, error_code: null, updated_at: null },
          },
          {
            role: 'main_fund_flow', label: '资金账号', adb: 'ONLINE', app: 'ONLINE', frida: 'ONLINE',
            lifecycle: { state: 'RUNNING', operation_id: null, error_code: null, updated_at: null },
          },
        ] })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    const user = userEvent.setup()
    render(<AdminPage />)
    const corePanel = (await screen.findByRole('heading', { name: '八项账号' })).closest('section')!
    const start = within(corePanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })
    await waitFor(() => expect(within(corePanel).getByText('生命周期：已关闭')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: '刷新运行端状态' }))
    await waitFor(() => expect(deviceReads).toBe(2))
    await user.click(start)
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '确认启动' }))
    await waitFor(() => expect(within(corePanel).getByText('生命周期：启动中')).toBeInTheDocument())

    resolveStaleRefresh(jsonResponse({ devices: [
      {
        role: 'core_metrics', label: '八项账号', adb: 'ONLINE', app: 'OFFLINE', frida: 'ONLINE',
        lifecycle: { state: 'STOPPED', operation_id: null, error_code: null, updated_at: null },
      },
      {
        role: 'main_fund_flow', label: '资金账号', adb: 'ONLINE', app: 'ONLINE', frida: 'ONLINE',
        lifecycle: { state: 'RUNNING', operation_id: null, error_code: null, updated_at: null },
      },
    ] }))

    await waitFor(() => expect(start).toHaveAttribute('aria-disabled', 'true'))
    expect(within(corePanel).getByText('生命周期：启动中')).toBeInTheDocument()
  })

  it('routes fund start and both account refreshes to their matching roles with card-local feedback', async () => {
    let resolveCoreRefresh!: (value: Response) => void
    const coreRefresh = new Promise<Response>((resolve) => { resolveCoreRefresh = resolve })
    mockAuthenticatedDashboard({ coreApp: 'OFFLINE' })
    const baseImplementation = vi.mocked(fetch).getMockImplementation()!
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/admin/devices/main_fund_flow/actions') return jsonResponse({ state: 'STARTING', operation_id: 'op-fund', error_code: null, updated_at: null }, 202)
      if (url === '/api/admin/account-sessions/core_metrics/refresh') return coreRefresh
      if (url === '/api/admin/account-sessions/main_fund_flow/refresh') return jsonResponse({ role: 'main_fund_flow', state: 'READY', updated_at: '2026-08-27T10:00:00+00:00', error_code: null })
      return baseImplementation(input, init)
    })
    const user = userEvent.setup()
    render(<AdminPage />)
    const corePanel = (await screen.findByRole('heading', { name: '八项账号' })).closest('section')!
    const fundPanel = screen.getByRole('heading', { name: '资金账号' }).closest('section')!
    expect(within(fundPanel).getByText('资金账号受保护：该操作不会切号、清数据、重装 App 或执行页面导航。')).toBeInTheDocument()

    const fundStart = within(fundPanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })
    await waitFor(() => expect(fundStart).toBeEnabled())
    await user.click(fundStart)
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '确认启动' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    await waitFor(() => expect(fundStart).toHaveFocus())
    expect(fundStart).toHaveAttribute('aria-disabled', 'true')
    expect(fetch).toHaveBeenCalledWith('/api/admin/devices/main_fund_flow/actions', expect.objectContaining({
      body: JSON.stringify({ action: 'start_and_launch_app' }),
    }))
    await user.click(fundStart)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.filter(([input]) => String(input) === '/api/admin/devices/main_fund_flow/actions')).toHaveLength(1)

    await user.click(within(corePanel).getByRole('button', { name: '刷新八项账号会话' }))
    expect(within(corePanel).getByRole('button', { name: '刷新中…' })).toBeDisabled()
    expect(within(fundPanel).getByRole('button', { name: '刷新资金账号会话' })).toBeEnabled()
    resolveCoreRefresh(jsonResponse({ detail: 'DIRECT_APP_OFFLINE' }, 503))
    await waitFor(() => expect(within(corePanel).getByRole('alert')).toHaveTextContent('DIRECT_APP_OFFLINE'))
    expect(within(fundPanel).queryByRole('alert')).not.toBeInTheDocument()
    await user.click(within(fundPanel).getByRole('button', { name: '刷新资金账号会话' }))
    await waitFor(() => expect(within(fundPanel).getByText('账号会话：已就绪')).toBeInTheDocument())
    expect(within(fundPanel).getByText(/更新时间：/)).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith('/api/admin/account-sessions/core_metrics/refresh', expect.objectContaining({ headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }) }))
    expect(fetch).toHaveBeenCalledWith('/api/admin/account-sessions/main_fund_flow/refresh', expect.objectContaining({ headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }) }))
  })

  it('uses lifecycle and live App health to enable only safe per-device actions', async () => {
    class FakeWebSocket {
      static instances = new Map<string, FakeWebSocket>()
      static OPEN = 1
      readyState = FakeWebSocket.OPEN
      close = vi.fn()
      send = vi.fn()
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor(public readonly url: string) { FakeWebSocket.instances.set(url, this) }
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const running = { state: 'RUNNING', operation_id: null, error_code: null, updated_at: null } as const
    const stopped = { ...running, state: 'STOPPED' as const }
    const unconfigured = { ...running, state: 'UNCONFIGURED' as const }
    render(<div className="admin-device-grid">
      <DeviceViewport role="core_metrics" title="运行设备" active locked lifecycle={running} streamUrl="ws://runner/running" />
      <DeviceViewport role="main_fund_flow" title="关闭设备" active locked lifecycle={stopped} streamUrl="ws://runner/stopped" />
      <DeviceViewport role="core_metrics" title="未配置设备" active locked lifecycle={unconfigured} streamUrl="ws://runner/unconfigured" />
    </div>)
    const runningSocket = FakeWebSocket.instances.get('ws://runner/running')!
    const runningPanel = screen.getByRole('heading', { name: '运行设备' }).closest('section')!
    const stoppedPanel = screen.getByRole('heading', { name: '关闭设备' }).closest('section')!
    const unconfiguredPanel = screen.getByRole('heading', { name: '未配置设备' }).closest('section')!
    runningSocket.onmessage?.({ data: JSON.stringify({ type: 'device_status', role: 'core_metrics', label: '运行设备', adb: 'ONLINE', app: 'OFFLINE', frida: 'ONLINE' }) } as MessageEvent)
    await waitFor(() => expect(within(runningPanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })).toHaveAttribute('aria-disabled', 'false'))
    runningSocket.onmessage?.({ data: JSON.stringify({ type: 'device_status', role: 'core_metrics', label: '运行设备', adb: 'ONLINE', app: 'ONLINE', frida: 'ONLINE' }) } as MessageEvent)
    await waitFor(() => expect(within(runningPanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })).toHaveAttribute('aria-disabled', 'true'))
    expect(within(runningPanel).getByRole('button', { name: '关闭虚拟机' })).toHaveAttribute('aria-disabled', 'false')
    expect(within(stoppedPanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })).toHaveAttribute('aria-disabled', 'false')
    expect(within(stoppedPanel).getByRole('button', { name: '关闭虚拟机' })).toHaveAttribute('aria-disabled', 'true')
    expect(within(unconfiguredPanel).getByRole('button', { name: '启动虚拟机并打开同花顺' })).toHaveAttribute('aria-disabled', 'true')
    expect(within(unconfiguredPanel).getByRole('button', { name: '关闭虚拟机' })).toHaveAttribute('aria-disabled', 'true')
  })

  it('restores a valid cookie session after a page refresh without browser storage', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: true }))
      .mockResolvedValueOnce(jsonResponse({ paused: true }))

    render(<AdminPage />)

    await waitFor(() => expect(screen.getByText('运行端就绪')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: '登录管理台' })).not.toBeInTheDocument()
    expect(fetch).toHaveBeenNthCalledWith(1, '/api/admin/session', expect.objectContaining({ credentials: 'include' }))
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/admin/runner', expect.objectContaining({ credentials: 'include' }))
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/admin/lock', expect.objectContaining({ credentials: 'include' }))
    expect(fetch).toHaveBeenNthCalledWith(4, '/api/admin/queue', expect.objectContaining({ credentials: 'include' }))
  })

  it('loads and creates ordinary market users from the administrator console', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ id: 9, username: 'trader', enabled: true, must_change_password: true, created_at: '2026-08-23T00:00:00+00:00' }, 201))

    const user = userEvent.setup()
    render(<AdminPage />)
    await user.click(await screen.findByRole('button', { name: '加载行情用户' }))
    await user.type(screen.getByLabelText('新用户名'), 'trader')
    await user.type(screen.getByLabelText('临时密码'), 'temporary-pass')
    await user.click(screen.getByRole('button', { name: '创建行情用户' }))

    expect(await screen.findByText('trader')).toBeInTheDocument()
    expect(screen.getByText('首次登录需改密')).toBeInTheDocument()
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/users', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
    }))
  })

  it('refreshes the fund account session without exposing session material', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(jsonResponse({
        role: 'main_fund_flow',
        state: 'READY',
        updated_at: '2026-08-27T10:00:00+00:00',
        error_code: null,
      }))

    const user = userEvent.setup()
    render(<AdminPage />)

    await user.click(await screen.findByRole('button', { name: '刷新资金账号会话' }))

    const fundPanel = screen.getByRole('heading', { name: '资金账号' }).closest('section')!
    await waitFor(() => expect(within(fundPanel).getByText('账号会话：已就绪')).toBeInTheDocument())
    expect(screen.queryByText(/private-cookie-marker|private-token-marker|private-user-agent-marker/i)).not.toBeInTheDocument()
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/account-sessions/main_fund_flow/refresh', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
    }))
  })

  it('does not block an existing cookie session while server validation is pending', () => {
    vi.mocked(fetch).mockReturnValue(new Promise<Response>(() => undefined))

    render(<AdminPage />)

    expect(screen.getByRole('button', { name: '退出管理台' })).toBeInTheDocument()
    expect(screen.queryByText('正在恢复管理会话…')).not.toBeInTheDocument()
  })

  it('invalidates authentication when post-auth device-session loading returns 401', async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input)
      if (url === '/api/admin/session') return new Response(null, { status: 204 })
      if (url === '/api/admin/runner') return jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false })
      if (url === '/api/admin/lock') return jsonResponse({ locked: false })
      if (url === '/api/admin/queue') return jsonResponse({ paused: false })
      if (url === '/api/admin/devices') return jsonResponse({ devices: [] })
      if (url === '/api/admin/account-sessions') return jsonResponse({ detail: 'admin authentication required' }, 401)
      throw new Error(`unexpected request: ${url}`)
    })

    render(<AdminPage />)

    expect(await screen.findByRole('button', { name: '登录管理台' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('管理会话已失效，请重新登录')
  })

  it('clears device, session, pending, error, and dialog state across logout and re-login', async () => {
    const never = new Promise<Response>(() => undefined)
    let deviceReads = 0
    let sessionReads = 0
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/admin/session' && init?.method === 'POST') return new Response(null, { status: 204 })
      if (url === '/api/admin/session') return new Response(null, { status: 204 })
      if (url === '/api/admin/session/logout') return new Response(null, { status: 204 })
      if (url === '/api/admin/runner') return jsonResponse({ state: 'ADMIN_CONTROL', last_heartbeat: null, queue_paused: true })
      if (url === '/api/admin/lock') return jsonResponse({ locked: true })
      if (url === '/api/admin/queue') return jsonResponse({ paused: true })
      if (url === '/api/admin/account-sessions/core_metrics/refresh') return never
      if (url === '/api/admin/devices') {
        deviceReads += 1
        if (deviceReads > 1) return never
        return jsonResponse({ devices: [
          {
            role: 'core_metrics', label: '八项账号', adb: 'ONLINE', app: 'OFFLINE', frida: 'ONLINE',
            lifecycle: { state: 'ERROR', operation_id: 'old-op', error_code: 'DEVICE_BOOT_TIMEOUT', updated_at: null },
          },
          {
            role: 'main_fund_flow', label: '资金账号', adb: 'ONLINE', app: 'ONLINE', frida: 'ONLINE',
            lifecycle: { state: 'RUNNING', operation_id: null, error_code: null, updated_at: null },
          },
        ] })
      }
      if (url === '/api/admin/account-sessions') {
        sessionReads += 1
        if (sessionReads > 1) return never
        return jsonResponse({ sessions: [
          { role: 'core_metrics', state: 'READY', updated_at: null, error_code: null },
          { role: 'main_fund_flow', state: 'READY', updated_at: null, error_code: null },
        ] })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    const user = userEvent.setup()
    render(<AdminPage />)
    const corePanel = (await screen.findByRole('heading', { name: '八项账号' })).closest('section')!
    await waitFor(() => expect(within(corePanel).getByRole('alert')).toHaveTextContent('DEVICE_BOOT_TIMEOUT'))
    await user.click(within(corePanel).getByRole('button', { name: '刷新八项账号会话' }))
    expect(within(corePanel).getByRole('button', { name: '刷新中…' })).toBeInTheDocument()
    await user.click(within(corePanel).getByRole('button', { name: '启动虚拟机并打开同花顺' }))
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '退出管理台' }))
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))

    const nextCorePanel = (await screen.findByRole('heading', { name: '八项账号' })).closest('section')!
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(within(nextCorePanel).getByText('生命周期：待检测')).toBeInTheDocument()
    expect(within(nextCorePanel).getByText('账号会话：未查询')).toBeInTheDocument()
    expect(within(nextCorePanel).getByRole('button', { name: '刷新八项账号会话' })).toBeInTheDocument()
    expect(within(nextCorePanel).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('logs in, shows the runner health, and acquires the operator lock', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'OFFLINE', last_heartbeat: null }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(jsonResponse({ locked: true }))

    const user = userEvent.setup()
    renderLoggedOutAdmin()
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))

    await waitFor(() => expect(screen.getByText('运行端离线')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: '接管设备' }))

    await waitFor(() => expect(screen.getByText('已获得设备控制权')).toBeInTheDocument())
    expect(screen.getByText('队列已暂停；已领取的任务会继续完成。')).toBeInTheDocument()
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/lock/acquire', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
    }))
    expect(screen.getByText('密码仅用于本次请求，不会记录或展示。')).toBeInTheDocument()
  })

  it('pauses the queue through the protected Runner endpoint', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'OFFLINE', last_heartbeat: null }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(jsonResponse({ paused: true }))

    const user = userEvent.setup()
    renderLoggedOutAdmin()
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))

    await waitFor(() => expect(screen.getByRole('button', { name: '暂停队列' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: '暂停队列' }))
    await waitFor(() => expect(screen.getByText('队列已暂停')).toBeInTheDocument())
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/queue/pause', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
    }))
  })

  it('logs out the administrator through the protected session endpoint', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))

    const user = userEvent.setup()
    renderLoggedOutAdmin()
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '退出管理台' })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: '退出管理台' }))

    await waitFor(() => expect(screen.getByRole('button', { name: '登录管理台' })).toBeInTheDocument())
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/session/logout', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
    }))
  })

  it('resumes a waiting task through the protected admin route', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'NEEDS_ADMIN', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(jsonResponse({ public_id: 'waiting-job', symbol: 'SZ.000001', status: 'QUEUED', error_code: null, created_at: '2026-08-21T00:00:00+00:00', captures: [] }))

    const user = userEvent.setup()
    renderLoggedOutAdmin()
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await user.type(screen.getByLabelText('等待任务 ID'), 'waiting-job')
    await user.click(screen.getByRole('button', { name: '恢复等待任务' }))

    await waitFor(() => expect(screen.getByText('任务已重新加入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/jobs/waiting-job/resume', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
    }))
  })

  it('retries a failed task through the protected admin route', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(jsonResponse({ public_id: 'failed-job', symbol: 'SZ.000001', status: 'QUEUED', error_code: null, created_at: '2026-08-21T00:00:00+00:00', captures: [] }))

    const user = userEvent.setup()
    renderLoggedOutAdmin()
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await user.type(screen.getByLabelText('失败任务 ID'), 'failed-job')
    await user.click(screen.getByRole('button', { name: '重试失败任务' }))

    await waitFor(() => expect(screen.getByText('失败任务已重新加入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/jobs/failed-job/retry', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
    }))
  })

  it('clears the local lock and closes the device stream when health refresh fails', async () => {
    class FakeWebSocket {
      static instance: FakeWebSocket | undefined
      static OPEN = 1
      readyState = FakeWebSocket.OPEN
      close = vi.fn()
      send = vi.fn()
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor(_url: string) { FakeWebSocket.instance = this }
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'ONLINE', last_heartbeat: null }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(jsonResponse({ locked: true }))
      .mockRejectedValueOnce(new Error('admin authentication required'))

    const user = userEvent.setup()
    renderLoggedOutAdmin('ws://runner.example/stream')
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await waitFor(() => expect(FakeWebSocket.instance).toBeDefined())
    await user.click(screen.getByRole('button', { name: '接管设备' }))
    await user.click(screen.getByRole('button', { name: '刷新运行端状态' }))

    await waitFor(() => expect(FakeWebSocket.instance!.close).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: '登录管理台' })).toBeInTheDocument()
  })

  it('keeps the admin viewport offline when its configured WebSocket URL is invalid', async () => {
    class InvalidWebSocket {
      constructor(_url: string) { throw new SyntaxError('Invalid URL') }
    }
    vi.stubGlobal('WebSocket', InvalidWebSocket)

    render(<DeviceViewport role="core_metrics" active locked streamUrl="not a websocket URL" />)

    await waitFor(() => expect(screen.getByText('设备画面连接不可用，当前离线')).toBeInTheDocument())
  })

  it('uses the same-origin device WebSocket when no override is configured', async () => {
    class FakeWebSocket {
      static instance: FakeWebSocket | undefined
      static OPEN = 1
      readyState = FakeWebSocket.OPEN
      close = vi.fn()
      send = vi.fn()
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor(public readonly url: string) { FakeWebSocket.instance = this }
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)

    render(<DeviceViewport role="core_metrics" active locked />)

    await waitFor(() => expect(FakeWebSocket.instance?.url).toBe(`ws://${window.location.host}/api/admin/device`))
  })

  it('keeps both account devices online simultaneously and warns against exiting the fund account', async () => {
    class FakeWebSocket {
      static urls: string[] = []
      static OPEN = 1
      readyState = FakeWebSocket.OPEN
      close = vi.fn()
      send = vi.fn()
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor(url: string) { FakeWebSocket.urls.push(url) }
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))

    render(<AdminPage />)

    expect(await screen.findByRole('heading', { name: '八项账号' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '资金账号' })).toBeInTheDocument()
    expect(screen.getByText('当前账号，禁止退出')).toBeInTheDocument()
    await waitFor(() => expect(FakeWebSocket.urls).toEqual(expect.arrayContaining([
      `ws://${window.location.host}/api/admin/devices/core_metrics`,
      `ws://${window.location.host}/api/admin/devices/main_fund_flow`,
    ])))
  })

  it('routes each panel controls through only its own websocket', async () => {
    class FakeWebSocket {
      static instances = new Map<string, FakeWebSocket>()
      static OPEN = 1
      readyState = FakeWebSocket.OPEN
      close = vi.fn()
      send = vi.fn()
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor(public readonly url: string) { FakeWebSocket.instances.set(url, this) }
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)
    render(<div className="admin-device-grid">
      <DeviceViewport role="core_metrics" title="八项账号" active locked streamUrl="ws://runner/core" />
      <DeviceViewport role="main_fund_flow" title="资金账号" active locked streamUrl="ws://runner/fund" />
    </div>)
    const coreSocket = FakeWebSocket.instances.get('ws://runner/core')!
    const fundSocket = FakeWebSocket.instances.get('ws://runner/fund')!
    for (const socket of [coreSocket, fundSocket]) {
      socket.onopen?.()
      socket.onmessage?.({ data: JSON.stringify({ type: 'runner_status', state: 'ADMIN_CONTROL', locked: true }) } as MessageEvent)
    }
    coreSocket.onmessage?.({ data: JSON.stringify({
      type: 'device_status', role: 'core_metrics', label: '八项账号', adb: 'ONLINE', app: 'ONLINE', frida: 'ONLINE',
    }) } as MessageEvent)
    fundSocket.onmessage?.({ data: JSON.stringify({
      type: 'device_status', role: 'main_fund_flow', label: '资金账号', adb: 'ONLINE', app: 'ONLINE', frida: 'OFFLINE',
    }) } as MessageEvent)

    const user = userEvent.setup()
    const corePanel = screen.getByRole('heading', { name: '八项账号' }).closest('section')!
    const fundPanel = screen.getByRole('heading', { name: '资金账号' }).closest('section')!
    await waitFor(() => expect(within(corePanel).getAllByText('在线')).toHaveLength(3))
    expect(within(fundPanel).getByText('离线')).toBeInTheDocument()
    await user.click(within(corePanel).getByRole('button', { name: '上翻' }))
    await user.click(within(fundPanel).getByRole('button', { name: '下翻' }))

    expect(coreSocket.send).toHaveBeenCalledTimes(1)
    expect(fundSocket.send).toHaveBeenCalledTimes(1)
    expect(coreSocket.send).toHaveBeenCalledWith(expect.stringContaining('"startY":0.3'))
    expect(fundSocket.send).toHaveBeenCalledWith(expect.stringContaining('"startY":0.72'))
  })

  it('provides explicit up and down controls that send normalized swipe events', async () => {
    class FakeWebSocket {
      static instance: FakeWebSocket | undefined
      static OPEN = 1
      readyState = FakeWebSocket.OPEN
      close = vi.fn()
      send = vi.fn()
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor(_url: string) { FakeWebSocket.instance = this }
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)

    render(<DeviceViewport role="core_metrics" active locked />)
    await waitFor(() => expect(FakeWebSocket.instance).toBeDefined())
    FakeWebSocket.instance!.onopen?.()
    FakeWebSocket.instance!.onmessage?.({
      data: JSON.stringify({ type: 'runner_status', state: 'ADMIN_CONTROL', locked: true }),
    } as MessageEvent)

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '上翻' }))
    await user.click(screen.getByRole('button', { name: '下翻' }))

    expect(FakeWebSocket.instance!.send).toHaveBeenNthCalledWith(1, JSON.stringify({
      type: 'input',
      sequence: 1,
      event: { kind: 'swipe', startX: 0.5, startY: 0.3, endX: 0.5, endY: 0.72 },
    }))
    expect(FakeWebSocket.instance!.send).toHaveBeenNthCalledWith(2, JSON.stringify({
      type: 'input',
      sequence: 2,
      event: { kind: 'swipe', startX: 0.5, startY: 0.72, endX: 0.5, endY: 0.3 },
    }))
  })

  it('keeps the device viewport active and explains a lock conflict', async () => {
    class ConnectingWebSocket {
      static OPEN = 1
      readyState = 0
      close = vi.fn()
      send = vi.fn()
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor(_url: string) { /* Remain in CONNECTING for this lock-conflict test. */ }
    }
    vi.stubGlobal('WebSocket', ConnectingWebSocket)
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'ADMIN_CONTROL', last_heartbeat: null, queue_paused: true }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: true }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'runner is locked by another admin' }, 409))

    const user = userEvent.setup()
    renderLoggedOutAdmin('ws://runner.example/stream')
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await user.click(screen.getByRole('button', { name: '接管设备' }))

    await waitFor(() => expect(screen.getByText('设备由其他管理会话控制，请先交还控制')).toBeInTheDocument())
    expect(screen.getAllByText('正在连接设备画面…')).toHaveLength(2)
    expect(screen.getByRole('button', { name: '接管设备' })).toBeInTheDocument()
  })

  it('marks administrator forms as ignored by password managers and submits password changes', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ devices: [] }))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))

    const user = userEvent.setup()
    renderLoggedOutAdmin()
    expect(document.querySelector('main[data-1p-ignore="true"]')).toBeInTheDocument()
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await user.type(screen.getByLabelText('当前管理员密码'), 'never-display-this')
    await user.type(screen.getByLabelText('新管理员密码'), 'new-admin-secret-123')
    await user.type(screen.getByLabelText('确认新管理员密码'), 'new-admin-secret-123')
    await user.click(screen.getByRole('button', { name: '修改管理员密码' }))

    await waitFor(() => expect(screen.getByRole('button', { name: '登录管理台' })).toBeInTheDocument())
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/password', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'test-csrf' }),
      body: JSON.stringify({
        current_password: 'never-display-this',
        new_password: 'new-admin-secret-123',
        new_password_confirmation: 'new-admin-secret-123',
      }),
    }))
  })
})
