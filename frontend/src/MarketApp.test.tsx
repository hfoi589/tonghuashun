import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MarketApp } from './MarketApp'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MarketApp', () => {
  it('shows the ordinary-user login when no market session exists', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'market authentication required' }, 401)))

    render(<MarketApp />)

    expect(await screen.findByRole('heading', { name: '登录行情中心' })).toBeInTheDocument()
    expect(screen.getByLabelText('用户名')).toBeInTheDocument()
    expect(screen.getByLabelText('密码')).toBeInTheDocument()
  })

  it('forces an administrator-issued temporary password to be replaced', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      id: 7,
      username: 'wilson',
      enabled: true,
      must_change_password: true,
      created_at: '2026-08-23T00:00:00+00:00',
    })))

    render(<MarketApp />)

    expect(await screen.findByRole('heading', { name: '设置你的新密码' })).toBeInTheDocument()
    expect(screen.getByLabelText('当前临时密码')).toBeInTheDocument()
  })

  it('loads grouped watchlists and opens a realtime stock detail', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7,
        username: 'wilson',
        enabled: true,
        must_change_password: false,
        created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{
        id: 1,
        name: '自选',
        sort_order: 0,
        items: [{ symbol: '601872', name: '招商轮船', market: '17' }],
      }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872',
        name: '招商轮船',
        market: '17',
        sequence: 12,
        source_time: '14:56',
        collected_at: '2026-08-23T06:56:01+00:00',
        stale: false,
        age_seconds: 0.4,
        quote: { price: '8.33', previous_close: '8.16', change_percent: '2.08%', turnover_rate: '1.42%' },
        timeshare: [
          { time: '09:30', price: '8.20', average_price: null, volume: '1200' },
          { time: '14:56', price: '8.33', average_price: null, volume: '900' },
        ],
        intraday_series: {},
        order_book: [],
        trades: [],
        main_fund_flow: {},
        capabilities: {
          timeshare: { available: true, reason: null },
          kline: { available: false, reason: 'DIRECT_KLINE_UNAVAILABLE' },
          order_book: { available: false, reason: 'APP_INTERFACE_NOT_CONFIRMED' },
          trades: { available: false, reason: 'APP_INTERFACE_NOT_CONFIRMED' },
        },
        source_errors: { core_metrics: null, main_fund_flow: null },
      })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    expect(await screen.findByRole('button', { name: /招商轮船/ })).toBeInTheDocument()
    expect((await screen.findAllByText('8.33')).length).toBeGreaterThan(0)
    expect(screen.getByRole('img', { name: '招商轮船分时价格图' })).toBeInTheDocument()
    expect(screen.getByText('14:56 更新')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '日K' }))
    await waitFor(() => expect(screen.getByText('App 内部 K 线接口尚未确认')).toBeInTheDocument())
  })
})
