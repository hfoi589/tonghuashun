import { cleanup, render, screen, waitFor } from '@testing-library/react'
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

describe('AdminPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    Object.defineProperty(document, 'cookie', { writable: true, value: 'ths_csrf=test-csrf' })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
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

  it('does not block an existing cookie session while server validation is pending', () => {
    vi.mocked(fetch).mockReturnValue(new Promise<Response>(() => undefined))

    render(<AdminPage />)

    expect(screen.getByRole('button', { name: '退出管理台' })).toBeInTheDocument()
    expect(screen.queryByText('正在恢复管理会话…')).not.toBeInTheDocument()
  })

  it('logs in, shows the runner health, and acquires the operator lock', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'OFFLINE', last_heartbeat: null }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
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

    render(<DeviceViewport active locked streamUrl="not a websocket URL" />)

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

    render(<DeviceViewport active locked />)

    await waitFor(() => expect(FakeWebSocket.instance?.url).toBe(`ws://${window.location.host}/api/admin/device`))
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

    render(<DeviceViewport active locked />)
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
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'ADMIN_CONTROL', last_heartbeat: null, queue_paused: true }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: true }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'runner is locked by another admin' }, 409))

    const user = userEvent.setup()
    renderLoggedOutAdmin('ws://runner.example/stream')
    await user.type(await screen.findByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await user.click(screen.getByRole('button', { name: '接管设备' }))

    await waitFor(() => expect(screen.getByText('设备由其他管理会话控制，请先交还控制')).toBeInTheDocument())
    expect(screen.getByText('正在连接设备画面…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '接管设备' })).toBeInTheDocument()
  })

  it('marks administrator forms as ignored by password managers and submits password changes', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'READY', last_heartbeat: null, queue_paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
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
