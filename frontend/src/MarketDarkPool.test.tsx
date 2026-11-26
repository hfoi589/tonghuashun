import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MarketApp, chartPeriods, formatQuoteVolume, isFundFlowPeriod } from './MarketApp'
import { MarketSessionContext } from './market-session'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('market dark-pool tab contract', () => {
  it('places 暗盘 first and keeps it as the default period', () => {
    expect(chartPeriods[0]).toEqual(['dark_pool', '暗盘'])
    expect(chartPeriods[1]).toEqual(['timeshare', '分时'])
  })

  it('shows fund flow only for the dark-pool period', () => {
    expect(isFundFlowPeriod('dark_pool')).toBe(true)
    expect(isFundFlowPeriod('timeshare')).toBe(false)
    expect(isFundFlowPeriod('day')).toBe(false)
  })

  it('hides duplicate brand chrome when embedded and short-circuits exchange-fund capital flow requests', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/watchlists') {
        return new Response(JSON.stringify({
          groups: [{
            id: 1,
            name: '自选',
            sort_order: 0,
            is_primary: true,
            items: [{ symbol: '513350', name: '标普油气ETF', market: '20' }],
          }],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      if (url.includes('/snapshot')) {
        return new Response(JSON.stringify({
          symbol: '513350', name: '标普油气ETF', market: '20', sequence: 1,
          source_time: '15:00', collected_at: '2026-11-23T07:00:00+00:00',
          stale: false, age_seconds: 0.2, source: 'TENCENT_PUBLIC', price_precision: 3,
          quote: { price: '1.214', change_percent: '-0.49%' },
          timeshare: [], intraday_series: {}, order_book: [], trades: [], main_fund_flow: {},
          capabilities: { timeshare: { available: false }, kline: { available: true }, l2: { available: false } },
          source_errors: {},
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    const { container } = render(
      <MarketSessionContext.Provider value={{
        user: { id: 1, username: 'wilson', enabled: true, must_change_password: false, role: 'admin', created_at: '2026-08-23T00:00:00+00:00' },
        logout: vi.fn(),
        watchSyncWarning: null,
      }}>
        <MarketApp embedded />
      </MarketSessionContext.Provider>,
    )

    expect(await screen.findByText('交易所基金（ETF/LOF）无同花顺主力与暗盘资金统计')).toBeInTheDocument()
    expect(container.querySelector('.market-wordmark')).not.toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/fund-flow/'))).toBe(false))
  })

  it('formats quote volume in 万手 and surfaces the 4 L2 metrics inside the quote band on dark-pool', async () => {
    expect(formatQuoteVolume('31250900')).toBe('31.25万手')
    expect(formatQuoteVolume('5200')).toBe('52手')

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/watchlists') {
        return new Response(JSON.stringify({
          groups: [{
            id: 1,
            name: '自选',
            sort_order: 0,
            is_primary: true,
            items: [{ symbol: '601872', name: '招商轮船', market: '17' }],
          }],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      if (url.includes('/snapshot')) {
        return new Response(JSON.stringify({
          symbol: '601872', name: '招商轮船', market: '17', sequence: 1,
          source_time: '11:30', collected_at: '2026-11-25T03:30:00+00:00',
          market_phase: 'BREAK', stale: false, age_seconds: 0.2, source: 'TENCENT_PUBLIC', price_precision: 2,
          quote: {
            price: '15.53', previous_close: '15.43', change_percent: '0.65%',
            open: '15.53', high: '15.76', low: '15.39', turnover_rate: '0.39%', volume: '31250900',
            large_order_net: '+0.03', large_order_amount: '3869.80万元', retail_count: '-4.17', macdfs: '-0.005',
          },
          timeshare: [], intraday_series: {}, order_book: [], trades: [],
          main_fund_flow: { today: { unit: '万元', main_net_inflow: '4545.33', main_visible_inflow: '3869.79', main_hidden_inflow: '675.53', retail_inflow: '-4545.33' } },
          capabilities: { timeshare: { available: true }, kline: { available: true }, l2: { available: true } },
          source_errors: {},
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      if (url.includes('/fund-flow/history')) {
        return new Response(JSON.stringify({
          symbol: '601872', name: '招商轮船', trade_date: '20261125', available_dates: ['20261125'],
          latest_sync_at: '2026-11-25T03:30:00+00:00', last_error: null,
          periods: { today: { points: [] }, three_day: { points: [] }, five_day: { points: [] } },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(
      <MarketSessionContext.Provider value={{
        user: { id: 1, username: 'wilson', enabled: true, must_change_password: false, role: 'admin', created_at: '2026-08-23T00:00:00+00:00' },
        logout: vi.fn(),
        watchSyncWarning: null,
      }}>
        <MarketApp embedded />
      </MarketSessionContext.Provider>,
    )

    expect(await screen.findByText('31.25万手')).toBeInTheDocument()
    const l2Strip = screen.getByLabelText('盘口L2核心指标')
    expect(within(l2Strip).getByText('大单净量')).toBeInTheDocument()
    expect(within(l2Strip).getByText('+0.03')).toBeInTheDocument()
    expect(within(l2Strip).getByText('3869.8万')).toBeInTheDocument()
    expect(within(l2Strip).getByText('MACDFS')).toBeInTheDocument()
  })
})
