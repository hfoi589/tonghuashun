import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Login } from './MarketApp'

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('Market registration and password reset', () => {
  const calls: Array<{ url: string, body: unknown }> = []
  beforeEach(() => {
    calls.length = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null })
      if (url === '/api/v1/registration') return json({ registration_enabled: true, password_reset_enabled: true })
      if (url === '/api/v1/register/code') return json({ sent: true }, 202)
      if (url === '/api/v1/register/verify') {
        const body = JSON.parse(String(init?.body)) as { code: string }
        return body.code === '123456' ? json({ ticket: 'T1' }) : json({ detail: '验证码错误或已过期' }, 400)
      }
      if (url === '/api/v1/register') return json({ id: 1, username: 'newuser', enabled: true, must_change_password: false, role: 'user', created_at: '2026-11-03T00:00:00Z' }, 201)
      throw new Error(`unexpected request: ${url}`)
    }))
  })
  afterEach(() => vi.unstubAllGlobals())

  it('blocks the password step until the emailed code is verified', async () => {
    const onLogin = vi.fn()
    const user = userEvent.setup()
    render(<Login onLogin={onLogin} />)
    await user.click(await screen.findByRole('button', { name: '注册账号' }))
    await user.type(screen.getByLabelText('用户名'), 'newuser')
    await user.type(screen.getByLabelText('邮箱地址'), 'new@example.com')
    await user.click(screen.getByRole('button', { name: '发送验证码' }))

    await user.type(await screen.findByLabelText('邮箱验证码'), '000000')
    await user.click(screen.getByRole('button', { name: '验证' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('验证码错误或已过期')
    expect(screen.queryByLabelText('设置密码')).not.toBeInTheDocument()

    await user.clear(screen.getByLabelText('邮箱验证码'))
    await user.type(screen.getByLabelText('邮箱验证码'), '123456')
    await user.click(screen.getByRole('button', { name: '验证' }))
    await user.type(await screen.findByLabelText('设置密码'), 'longenough1')
    await user.type(screen.getByLabelText('确认密码'), 'longenough1')
    await user.click(screen.getByRole('button', { name: '完成注册并登录' }))

    await vi.waitFor(() => expect(onLogin).toHaveBeenCalledWith(expect.objectContaining({ username: 'newuser' })))
    expect(calls.find((call) => call.url === '/api/v1/register')?.body).toEqual({ ticket: 'T1', password: 'longenough1', password_confirmation: 'longenough1' })
  })
})
