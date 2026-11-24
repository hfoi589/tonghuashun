import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MarketApp, chartPeriods, isFundFlowPeriod } from './MarketApp'
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
})
