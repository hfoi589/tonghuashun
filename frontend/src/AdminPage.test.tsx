import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AdminPage } from './AdminPage'

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
})
