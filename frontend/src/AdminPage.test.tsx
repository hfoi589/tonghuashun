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

  it('logs in, shows the runner health, and acquires the operator lock', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ state: 'OFFLINE', last_heartbeat: null }))
      .mockResolvedValueOnce(jsonResponse({ locked: false }))
      .mockResolvedValueOnce(jsonResponse({ paused: false }))
      .mockResolvedValueOnce(jsonResponse({ locked: true }))

    const user = userEvent.setup()
    render(<AdminPage />)
    await user.type(screen.getByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))

    await waitFor(() => expect(screen.getByText('运行端离线')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: '接管设备' }))

    await waitFor(() => expect(screen.getByText('已获得设备控制权')).toBeInTheDocument())
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
    render(<AdminPage />)
    await user.type(screen.getByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))

    await waitFor(() => expect(screen.getByRole('button', { name: '暂停队列' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: '暂停队列' }))
    await waitFor(() => expect(screen.getByText('队列已暂停')).toBeInTheDocument())
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/queue/pause', expect.objectContaining({
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
    render(<AdminPage />)
    await user.type(screen.getByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await user.type(screen.getByLabelText('等待任务 ID'), 'waiting-job')
    await user.click(screen.getByRole('button', { name: '恢复等待任务' }))

    await waitFor(() => expect(screen.getByText('任务已重新加入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenLastCalledWith('/api/admin/jobs/waiting-job/resume', expect.objectContaining({
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
    render(<AdminPage deviceStreamUrl="ws://runner.example/stream" />)
    await user.type(screen.getByLabelText('管理员密码'), 'never-display-this')
    await user.click(screen.getByRole('button', { name: '登录管理台' }))
    await waitFor(() => expect(FakeWebSocket.instance).toBeDefined())
    await user.click(screen.getByRole('button', { name: '接管设备' }))
    await user.click(screen.getByRole('button', { name: '刷新运行端状态' }))

    await waitFor(() => expect(FakeWebSocket.instance!.close).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: '接管设备' })).toBeInTheDocument()
  })

  it('keeps the admin viewport offline when its configured WebSocket URL is invalid', async () => {
    class InvalidWebSocket {
      constructor(_url: string) { throw new SyntaxError('Invalid URL') }
    }
    vi.stubGlobal('WebSocket', InvalidWebSocket)

    render(<DeviceViewport active locked streamUrl="not a websocket URL" />)

    await waitFor(() => expect(screen.getByText('设备画面连接不可用，当前离线')).toBeInTheDocument())
  })
})
