import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MarketApp, marketPhaseLabel, marketSourceLabel, nextIntradaySelectedTime } from './MarketApp'

vi.mock('./DailyKChart', () => ({
  DailyKChart: ({ name, page, onSelectionChange }: {
    name: string,
    page: { symbol: string, bars: Array<Record<string, string | null>> },
    onSelectionChange?: (selection: { index: number, bar: Record<string, string | null> }) => void,
  }) => <div>
    <div role="img" aria-label={`${name}前复权日K图`}>{page.symbol}</div>
    <button type="button" onClick={() => onSelectionChange?.({ index: 0, bar: page.bars[0] })}>选择前一交易日</button>
  </div>,
}))

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('MarketApp', () => {
  it('labels auction and continuous quote sources by market phase', () => {
    expect(marketPhaseLabel('CALL_AUCTION')).toBe('集合竞价')
    expect(marketSourceLabel('THS_AUCTION', 'CALL_AUCTION')).toBe('同花顺集合竞价')
    expect(marketSourceLabel('THS_DIRECT_QUOTE', 'PREOPEN_QUOTE')).toBe('同花顺盘前直连')
    expect(marketSourceLabel('TENCENT_PUBLIC', 'CONTINUOUS')).toBe('腾讯公开行情')
  })

  it('gives the new-group button readable foreground and background colors', () => {
    const root = document.documentElement
    const previousRed = root.style.getPropertyValue('--market-red')
    root.style.setProperty('--market-red', '#e2232e')
    const header = document.createElement('div')
    header.className = 'market-watchlist-head'
    const button = document.createElement('button')
    header.append(button)
    document.body.append(header)

    try {
      const style = getComputedStyle(button)
      expect(style.backgroundColor).toBe('rgb(226, 35, 46)')
      expect(style.color).toBe('rgb(255, 255, 255)')
    } finally {
      header.remove()
      if (previousRed) root.style.setProperty('--market-red', previousRed)
      else root.style.removeProperty('--market-red')
    }
  })

  it('resets an obsolete intraday selection when a new trading-day stream starts', () => {
    expect(nextIntradaySelectedTime('14:00', '15:00', ['09:30'])).toBe('09:30')
    expect(nextIntradaySelectedTime('10:00', '10:30', ['09:30', '10:00', '10:31'])).toBe('10:00')
    expect(nextIntradaySelectedTime('10:30', '10:30', ['09:30', '10:30', '10:31'])).toBe('10:31')
  })

  it('shows the ordinary-user login when no market session exists', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'market authentication required' }, 401)))

    render(<MarketApp />)

    expect(await screen.findByRole('heading', { name: '登录行情中心' })).toBeInTheDocument()
    expect(screen.getByLabelText('用户名')).toBeInTheDocument()
    expect(screen.getByLabelText('密码')).toBeInTheDocument()
  })

  it('returns to the login form when the market session expires after loading', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7,
        username: 'wilson',
        enabled: true,
        must_change_password: false,
        created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return jsonResponse({ detail: 'market authentication required' }, 401)
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<MarketApp />)

    expect(await screen.findByRole('heading', { name: '登录行情中心' })).toBeInTheDocument()
    expect(screen.getByLabelText('用户名')).toBeInTheDocument()
    expect(screen.getByLabelText('密码')).toBeInTheDocument()
    expect(screen.queryByText('我的自选')).not.toBeInTheDocument()
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

  it('renders public fund precision and hides unavailable direct L2 sections', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7, username: 'wilson', enabled: true, must_change_password: false,
        created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{
        id: 1, name: '自选', sort_order: 0, is_primary: true,
        items: [{ symbol: '510300', name: '沪深300ETF', market: '20' }],
      }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '510300', name: '沪深300ETF', market: '20', sequence: 1,
        source_time: '15:00', collected_at: '2026-08-27T07:00:00+00:00',
        stale: false, age_seconds: 0.2, source: 'TENCENT_PUBLIC', price_precision: 3,
        quote: { price: '4.123', change_percent: '0.56%', open: '4.101', high: '4.130', low: '4.090', previous_close: '4.100' },
        timeshare: [{ time: '09:30', price: '4.123', average_price: '4.123', volume: '10000' }],
        intraday_series: {}, order_book: [], trades: [], main_fund_flow: {},
        capabilities: {
          timeshare: { available: true }, kline: { available: true, adjustment: 'qfq' },
          daily_kline: { available: true, adjustment: 'qfq' },
          l2: { available: false, reason: 'DIRECT_SESSION_UNAVAILABLE' },
          order_book: { available: false, reason: 'PUBLIC_LEVEL2_UNAVAILABLE' },
          trades: { available: false, reason: 'PUBLIC_LEVEL2_UNAVAILABLE' },
        },
        source_errors: { tencent_public: null, sina_public: null, core_metrics: 'DIRECT_SESSION_UNAVAILABLE', main_fund_flow: null },
      })
      if (url.includes('/series?period=day')) return jsonResponse({
        symbol: '510300', period: 'day', bars: [], indicators: {}, next_cursor: null,
        source_error: 'KLINE_SOURCES_UNAVAILABLE', adjustment: 'qfq', source: null,
        cached: false, stale: false,
        source_errors: { ths_public_kline: 'PUBLIC_KLINE_HTTP_ERROR', tencent_public_kline: 'PUBLIC_KLINE_UNAVAILABLE' },
      })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    await userEvent.click(await screen.findByRole('button', { name: '暗盘' }))
    await userEvent.click(screen.getByRole('button', { name: '分时' }))
    const chart = await screen.findByRole('img', { name: '沪深300ETF分时价格图' })
    expect(within(chart.closest('figure')!).getByText('4.123', { selector: '.market-timeshare-readout strong' })).toBeInTheDocument()
    expect(screen.queryByText('大单净量')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '主力流向' })).not.toBeInTheDocument()
    expect(screen.queryByText('当前 App 接口未确认')).not.toBeInTheDocument()
    expect(screen.getByText('腾讯公开行情')).toBeInTheDocument()
  })

  it('keeps fund flow visible when core eight-metric enrichment is unavailable', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({ id: 7, username: 'wilson', enabled: true, must_change_password: false, created_at: '2026-08-23T00:00:00+00:00' })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{ id: 1, name: '自选', sort_order: 0, is_primary: true, items: [{ symbol: '601872', name: '招商轮船', market: '17' }] }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872', name: '招商轮船', market: '17', sequence: 1, source_time: '09:31', collected_at: '2026-08-31T01:31:00+00:00',
        source: 'TENCENT_PUBLIC', price_precision: 2, stale: false, age_seconds: 0.2,
        quote: { price: '10.20', change_percent: '2.00%' }, timeshare: [{ time: '09:31', price: '10.20', average_price: '10.10', volume: '1000' }],
        intraday_series: {}, order_book: [], trades: [],
        main_fund_flow: {
          today: { unit: '万元', main_net_inflow: '12000.00', main_visible_inflow: '8000.00', main_hidden_inflow: '4000.00', retail_inflow: '-12000.00' },
          three_day: { unit: '万元', main_net_inflow: '13000.00', main_visible_inflow: '9000.00', main_hidden_inflow: '4000.00', retail_inflow: '-13000.00' },
          five_day: { unit: '亿元', main_net_inflow: '1.20', main_visible_inflow: '0.80', main_hidden_inflow: '0.40', retail_inflow: '-1.20' },
        },
        capabilities: { timeshare: { available: true }, kline: { available: true }, l2: { available: false, reason: 'DIRECT_PROTOCOL_RESPONSE_TIMEOUT' } },
        source_errors: { core_metrics: 'DIRECT_PROTOCOL_RESPONSE_TIMEOUT', main_fund_flow: null },
      })
      if (url.includes('/series?period=day')) return jsonResponse({ symbol: '601872', period: 'day', bars: [], indicators: {}, next_cursor: null, source_error: null, adjustment: 'qfq', source: 'TENCENT_PUBLIC', cached: false, stale: false, source_errors: {} })
      if (url.includes('/fund-flow/history')) return jsonResponse({ symbol: '601872', name: '招商轮船', trade_date: '20260831', available_dates: ['20260831'], latest_sync_at: '2026-08-31T01:31:00+00:00', last_error: null, periods: {
        today: { points: [{ time: '09:31', unit: '万元', main_net_inflow: '999.00', main_visible_inflow: '8000.00', main_hidden_inflow: '4000.00', retail_inflow: '-12000.00' }] },
        three_day: { points: [] },
        five_day: { points: [] },
      } })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    expect(await screen.findByRole('heading', { name: '主力流向' })).toBeInTheDocument()
    expect(screen.getAllByText('4000.00').length).toBeGreaterThan(0)
    expect(within(screen.getByRole('heading', { name: '主力流向' }).closest('section')!).getByText('统一单位：万元')).toBeInTheDocument()
    expect(await screen.findByTestId('fund-flow-readout-main-net')).toHaveTextContent('999.00')
    expect(screen.queryByTestId('fund-flow-active-label-main-net')).not.toBeInTheDocument()
    expect(screen.queryByText('大单净量')).not.toBeInTheDocument()
    expect(screen.queryByText('核心八项直连暂不可用')).not.toBeInTheDocument()
  })

  it('updates the left fund table when the history trade date changes', async () => {
    const historyResponse = (tradeDate: string, values: [string, string, string]) => ({
      symbol: '601872',
      name: '招商轮船',
      trade_date: tradeDate,
      available_dates: ['20260916', '20260915'],
      latest_sync_at: '2026-09-16T07:00:00+00:00',
      last_error: null,
      periods: {
        today: { points: [{ time: '15:00', unit: '万元', main_net_inflow: values[0], main_visible_inflow: '1', main_hidden_inflow: '2', retail_inflow: `-${values[0]}` }] },
        three_day: { points: [{ time: '15:00', unit: '万元', main_net_inflow: values[1], main_visible_inflow: '3', main_hidden_inflow: '4', retail_inflow: `-${values[1]}` }] },
        five_day: { points: [{ time: '15:00', unit: '万元', main_net_inflow: values[2], main_visible_inflow: '5', main_hidden_inflow: '6', retail_inflow: `-${values[2]}` }] },
      },
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({ id: 7, username: 'wilson', enabled: true, must_change_password: false, created_at: '2026-08-23T00:00:00+00:00' })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{ id: 1, name: '自选', sort_order: 0, is_primary: true, items: [{ symbol: '601872', name: '招商轮船', market: '17' }] }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872', name: '招商轮船', market: '17', sequence: 1, source_time: '15:00', collected_at: '2026-09-16T07:00:00+00:00',
        source: 'TENCENT_PUBLIC', price_precision: 2, stale: false, age_seconds: 0.2,
        quote: { price: '19.86', change_percent: '-3.12%' }, timeshare: [], intraday_series: {}, order_book: [], trades: [],
        main_fund_flow: {
          today: { unit: '万元', main_net_inflow: '111', main_visible_inflow: '11', main_hidden_inflow: '12', retail_inflow: '-111' },
          three_day: { unit: '万元', main_net_inflow: '222', main_visible_inflow: '21', main_hidden_inflow: '22', retail_inflow: '-222' },
          five_day: { unit: '万元', main_net_inflow: '333', main_visible_inflow: '31', main_hidden_inflow: '32', retail_inflow: '-333' },
        },
        capabilities: { l2: { available: true } }, source_errors: {},
      })
      if (url.endsWith('/fund-flow/history')) return jsonResponse(historyResponse('20260916', ['100', '200', '300']))
      if (url.includes('/fund-flow/history?trade_date=20260915')) return jsonResponse(historyResponse('20260915', ['10', '20', '30']))
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    const dateSelect = await screen.findByRole('combobox', { name: '选择资金流交易日' })
    await userEvent.selectOptions(dateSelect, '20260915')
    const fundSection = screen.getByRole('heading', { name: '主力流向' }).closest('section')!
    expect(fundSection.querySelector('.market-fund-table-column > .market-fund-column-head')).toBeInTheDocument()
    expect(fundSection.querySelector('.market-fund-tablist')).toBeInTheDocument()
    expect(fundSection.querySelector('.market-fund-table-head')).toBeInTheDocument()
    const netRow = within(fundSection).getByRole('row', { name: /主力净流入/ })

    await waitFor(() => expect(within(netRow).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      '10.00',
      '20.00',
      '30.00',
    ]))
  })

  it('replaces the dark-pool K-line placeholder with the latest 30-day fund-flow chart', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({ id: 7, username: 'wilson', enabled: true, must_change_password: false, created_at: '2026-08-23T00:00:00+00:00' })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{ id: 1, name: '自选', sort_order: 0, is_primary: true, items: [{ symbol: '601872', name: '招商轮船', market: '17' }] }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872', name: '招商轮船', market: '17', sequence: 1, source_time: '15:00', collected_at: '2026-09-16T07:00:00+00:00',
        source: 'TENCENT_PUBLIC', price_precision: 2, stale: false, age_seconds: 0.2,
        quote: { price: '19.86', change_percent: '-3.12%' }, timeshare: [], intraday_series: {}, order_book: [], trades: [],
        main_fund_flow: { today: { unit: '万元', main_net_inflow: '10', main_visible_inflow: '6', main_hidden_inflow: '4', retail_inflow: '-10' } },
        capabilities: { l2: { available: true }, kline: { available: true } }, source_errors: {},
      })
      if (url.endsWith('/fund-flow/history')) return jsonResponse({
        symbol: '601872', name: '招商轮船', trade_date: '20260916', available_dates: ['20260916'], latest_sync_at: null, last_error: null,
        periods: { today: { points: [{ time: '15:00', unit: '万元', main_net_inflow: '10', main_visible_inflow: '6', main_hidden_inflow: '4', retail_inflow: '-10' }] }, three_day: { points: [] }, five_day: { points: [] } },
      })
      if (url.includes('/fund-flow/daily')) return jsonResponse({
        symbol: '601872', name: '招商轮船', limit: 30,
        points: [
          { trade_date: '20260915', time: '15:00', unit: '万元', main_net_inflow: '8', main_visible_inflow: '5', main_hidden_inflow: '3', retail_inflow: '-8' },
          { trade_date: '20260916', time: '15:00', unit: '万元', main_net_inflow: '10', main_visible_inflow: '6', main_hidden_inflow: '4', retail_inflow: '-10' },
        ],
      })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    await userEvent.click(await screen.findByRole('tab', { name: '近30日资金流向' }))
    expect(await screen.findByRole('img', { name: '最近30个交易日资金流向折线图' })).toBeInTheDocument()
    expect(screen.queryByText('暂无公开 K 线数据')).not.toBeInTheDocument()
  })

  it('switches fund-flow tabs and overlays the selected 30-day periods in the table', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({ id: 7, username: 'wilson', enabled: true, must_change_password: false, created_at: '2026-08-23T00:00:00+00:00' })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{ id: 1, name: '自选', sort_order: 0, is_primary: true, items: [{ symbol: '601872', name: '招商轮船', market: '17' }] }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872', name: '招商轮船', market: '17', sequence: 1, source_time: '15:00', collected_at: '2026-09-16T07:00:00+00:00',
        source: 'TENCENT_PUBLIC', price_precision: 2, stale: false, age_seconds: 0.2,
        quote: { price: '19.86', change_percent: '-3.12%' }, timeshare: [], intraday_series: {}, order_book: [], trades: [],
        main_fund_flow: {
          today: { unit: '万元', main_net_inflow: '111', main_visible_inflow: '11', main_hidden_inflow: '12', retail_inflow: '-111' },
          three_day: { unit: '万元', main_net_inflow: '222', main_visible_inflow: '21', main_hidden_inflow: '22', retail_inflow: '-222' },
          five_day: { unit: '万元', main_net_inflow: '333', main_visible_inflow: '31', main_hidden_inflow: '32', retail_inflow: '-333' },
        },
        capabilities: { l2: { available: true }, kline: { available: true } }, source_errors: {},
      })
      if (url.endsWith('/fund-flow/history')) return jsonResponse({
        symbol: '601872', name: '招商轮船', trade_date: '20260916', available_dates: ['20260916'], latest_sync_at: null, last_error: null,
        periods: {
          today: { points: [{ time: '15:00', unit: '万元', main_net_inflow: '100', main_visible_inflow: '10', main_hidden_inflow: '20', retail_inflow: '-100' }] },
          three_day: { points: [{ time: '15:00', unit: '万元', main_net_inflow: '200', main_visible_inflow: '20', main_hidden_inflow: '30', retail_inflow: '-200' }] },
          five_day: { points: [{ time: '15:00', unit: '万元', main_net_inflow: '300', main_visible_inflow: '30', main_hidden_inflow: '40', retail_inflow: '-300' }] },
        },
      })
      if (url.includes('/fund-flow/daily')) return jsonResponse({
        symbol: '601872', name: '招商轮船', limit: 30,
        points: [
          { trade_date: '20260914', time: '00:00', unit: '万元', main_net_inflow: '0', main_visible_inflow: '0', main_hidden_inflow: '0', retail_inflow: '0', periods: null },
          {
            trade_date: '20260915', time: '15:00', unit: '万元', main_net_inflow: '8', main_visible_inflow: '5', main_hidden_inflow: '3', retail_inflow: '-8',
            periods: {
              today: { unit: '万元', main_net_inflow: '801', main_visible_inflow: '81', main_hidden_inflow: '82', retail_inflow: '-801' },
              three_day: { unit: '万元', main_net_inflow: '802', main_visible_inflow: '83', main_hidden_inflow: '84', retail_inflow: '-802' },
              five_day: { unit: '万元', main_net_inflow: '805', main_visible_inflow: '85', main_hidden_inflow: '86', retail_inflow: '-805' },
            },
          },
        ],
      })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    expect(await screen.findByRole('tab', { name: '当天资金曲线' })).toHaveAttribute('aria-selected', 'true')
    const fundSection = screen.getByRole('heading', { name: '主力流向' }).closest('section')!
    const netRow = within(fundSection).getByRole('row', { name: /主力净流入/ })
    await waitFor(() => expect(within(netRow).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      '100.00', '200.00', '300.00',
    ]))

    await userEvent.click(screen.getByRole('tab', { name: '近30日资金流向' }))
    const chart = await screen.findByRole('img', { name: '最近30个交易日资金流向折线图' })
    await waitFor(() => expect(within(netRow).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      '801.00', '802.00', '805.00',
    ]))

    fireEvent.keyDown(chart, { key: 'ArrowLeft' })
    await waitFor(() => expect(within(netRow).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      '—', '—', '—',
    ]))

    await userEvent.click(screen.getByRole('tab', { name: '当天资金曲线' }))
    await waitFor(() => expect(within(netRow).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      '100.00', '200.00', '300.00',
    ]))
  })

  it('reconnects the market stream, resubscribes, and refreshes HTTP snapshot', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7, username: 'wilson', enabled: true, must_change_password: false,
        created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{
        id: 1, name: '自选', sort_order: 0, is_primary: true,
        items: [{ symbol: '601872', name: '招商轮船', market: '17' }],
      }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872', name: '招商轮船', market: '17', sequence: 1,
        source_time: '15:00', collected_at: '2026-08-27T07:00:00+00:00',
        source: 'TENCENT_PUBLIC', price_precision: 2, stale: false, age_seconds: 0.2,
        quote: { price: '18.62', change_percent: '1.31%' },
        timeshare: [{ time: '15:00', price: '18.62', average_price: '18.40', volume: '1000' }],
        intraday_series: {}, order_book: [], trades: [], main_fund_flow: {},
        capabilities: { timeshare: { available: true }, kline: { available: true }, l2: { available: false } },
        source_errors: { core_metrics: null, main_fund_flow: null },
      })
      if (url.includes('/series?period=day')) return jsonResponse({
        symbol: '601872', period: 'day', bars: [], indicators: {}, next_cursor: null,
        source_error: 'KLINE_SOURCES_UNAVAILABLE', adjustment: 'qfq', source: null,
        cached: false, stale: false, source_errors: {},
      })
      throw new Error(`unexpected request: ${url}`)
    })
    class FakeWebSocket {
      static instances: FakeWebSocket[] = []
      readyState = 1
      onopen: ((event: Event) => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onclose: ((event: CloseEvent) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      send = vi.fn()
      close = vi.fn()
      constructor(public url: string) { FakeWebSocket.instances.push(this) }
    }
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)

    render(<MarketApp />)

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    FakeWebSocket.instances[0].onopen?.(new Event('open'))
    expect(FakeWebSocket.instances[0].send).toHaveBeenCalledWith(JSON.stringify({
      type: 'subscribe', watchlist: ['601872'], detail: '601872',
    }))

    FakeWebSocket.instances[0].onclose?.(new CloseEvent('close'))

    expect(await screen.findByText('正在重连')).toBeInTheDocument()
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2), { timeout: 1600 })
    FakeWebSocket.instances[1].onopen?.(new Event('open'))
    expect(FakeWebSocket.instances[1].send).toHaveBeenCalledWith(JSON.stringify({
      type: 'subscribe', watchlist: ['601872'], detail: '601872',
    }))
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/snapshot')).length).toBeGreaterThan(1)
  })

  it('requests the selected snapshot after watchlists load even when the stream is already live', async () => {
    let resolveWatchlists: ((response: Response) => void) | undefined
    const watchlistsResponse = new Promise<Response>((resolve) => { resolveWatchlists = resolve })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7, username: 'wilson', enabled: true, must_change_password: false,
        created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return watchlistsResponse
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872', name: '招商轮船', market: '17', sequence: 1,
        source_time: '15:00', collected_at: '2026-08-27T07:00:00+00:00',
        source: 'TENCENT_PUBLIC', price_precision: 2, stale: false, age_seconds: 0.2,
        quote: { price: '18.62', change_percent: '1.31%' },
        timeshare: [{ time: '15:00', price: '18.62', average_price: '18.40', volume: '1000' }],
        intraday_series: {}, order_book: [], trades: [], main_fund_flow: {},
        capabilities: { timeshare: { available: true }, kline: { available: true }, l2: { available: false } },
        source_errors: { core_metrics: null, main_fund_flow: null },
      })
      if (url.includes('/fund-flow/history')) return jsonResponse({
        symbol: '601872', name: '招商轮船', trade_date: null, available_dates: [], latest_sync_at: null, last_error: null,
        periods: { today: { points: [] }, three_day: { points: [] }, five_day: { points: [] } },
      })
      throw new Error(`unexpected request: ${url}`)
    })
    class FakeWebSocket {
      static instances: FakeWebSocket[] = []
      readyState = 1
      onopen: ((event: Event) => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onclose: ((event: CloseEvent) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      send = vi.fn()
      close = vi.fn()
      constructor(public url: string) { FakeWebSocket.instances.push(this) }
    }
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)

    render(<MarketApp />)
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    FakeWebSocket.instances[0].onopen?.(new Event('open'))

    resolveWatchlists?.(jsonResponse({ groups: [{
      id: 1, name: '自选', sort_order: 0, is_primary: true,
      items: [{ symbol: '601872', name: '招商轮船', market: '17' }],
    }] }))

    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/snapshot'))).toHaveLength(1))
    expect((await screen.findAllByText('18.62')).length).toBeGreaterThan(0)
  })

  it('keeps one market stream while changing the selected stock', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7, username: 'wilson', enabled: true, must_change_password: false,
        created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{
        id: 1, name: '自选', sort_order: 0, is_primary: true,
        items: [
          { symbol: '601872', name: '招商轮船', market: '17' },
          { symbol: '600026', name: '中远海能', market: '17' },
        ],
      }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: url.includes('600026') ? '600026' : '601872',
        name: url.includes('600026') ? '中远海能' : '招商轮船',
        market: '17', sequence: 1, source_time: '10:00',
        collected_at: '2026-08-23T02:00:00+00:00', source: 'TENCENT_PUBLIC',
        price_precision: 2, stale: false, age_seconds: 0.2,
        quote: { price: '18.62', change_percent: '1.31%' },
        timeshare: [{ time: '10:00', price: '18.62', average_price: '18.40', volume: '1000' }],
        intraday_series: {}, order_book: [], trades: [], main_fund_flow: {},
        capabilities: { timeshare: { available: true }, kline: { available: true }, l2: { available: false } },
        source_errors: { core_metrics: null, main_fund_flow: null },
      })
      if (url.includes('/series?period=day')) return jsonResponse({
        symbol: '601872', period: 'day', bars: [], indicators: {}, next_cursor: null,
        source_error: 'KLINE_SOURCES_UNAVAILABLE', adjustment: 'qfq', source: null,
        cached: false, stale: false, source_errors: {},
      })
      throw new Error(`unexpected request: ${url}`)
    })
    class FakeWebSocket {
      static instances: FakeWebSocket[] = []
      readyState = 1
      onopen: ((event: Event) => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onclose: ((event: CloseEvent) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      send = vi.fn()
      close = vi.fn()
      constructor(public url: string) { FakeWebSocket.instances.push(this) }
    }
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)

    render(<MarketApp />)
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    FakeWebSocket.instances[0].onopen?.(new Event('open'))

    await userEvent.click(await screen.findByRole('button', { name: /中远海能/ }))

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    expect(FakeWebSocket.instances[0].send).toHaveBeenLastCalledWith(JSON.stringify({
      type: 'subscribe', watchlist: ['601872', '600026'], detail: '600026',
    }))
  })

  it('opens monitoring and replay inside the market detail page', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7, username: 'wilson', enabled: true, must_change_password: false,
        created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{
        id: 1, name: '自选', sort_order: 0, is_primary: true,
        items: [{ symbol: '601872', name: '招商轮船', market: '17' }],
      }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872', name: '招商轮船', market: '17', sequence: 1,
        source_time: '09:31', collected_at: '2026-08-31T01:31:00+00:00',
        source: 'TENCENT_PUBLIC', price_precision: 2, stale: false, age_seconds: 0.2,
        quote: { price: '10.20', change_percent: '2.00%' },
        timeshare: [{ time: '09:31', price: '10.20', average_price: '10.10', volume: '1000' }],
        intraday_series: {}, order_book: [], trades: [], main_fund_flow: {},
        capabilities: { timeshare: { available: true }, kline: { available: true }, l2: { available: false } },
        source_errors: {},
      })
      if (url.includes('/series?period=day')) return jsonResponse({
        symbol: '601872', period: 'day', bars: [], indicators: {}, next_cursor: null,
        source_error: null, adjustment: 'qfq', source: 'TENCENT_PUBLIC', cached: false, stale: false, source_errors: {},
      })
      if (url === '/api/v1/monitoring') return jsonResponse([{ symbol: '601872', enabled: true }])
      if (url === '/api/v1/monitoring/portfolio') return jsonResponse({ items: [{ symbol: '601872', name: '招商轮船', enabled: true, latest_price: '10.20', latest_time: '09:31', latest_trade_date: '20260831', last_sync_at: '2026-08-31T09:31:00+08:00', last_error: null, latest_signal: { side: 'buy', rule_title: '买入' } }] })
      if (url === '/api/v1/monitoring/premium') return jsonResponse({ as_of: '2026-08-31T09:31:00+08:00', valid_count: 1, errors: {}, rows: [{ code: '160723', name: '嘉实原油', current_price: 1.2, estimated_price: 1.0, premium_rate: 0.2, stale: false }] })
      if (url === '/api/v1/replay/rules/enabled') return jsonResponse([{ id: 'buy', title: '买入', enabled: true, side: 'buy', joiner: 'and', conditions: [] }])
      if (url === '/api/v1/replay/601872') return jsonResponse({
        symbol: '601872', name: '招商轮船', trade_date: '20260831', source: 'TENCENT_PUBLIC',
        available_dates: ['20260831'], macd_settings: { short: 10, long: 20, signal: 5, marker_threshold: 0.001 },
        points: [{ time: '09:31', price: '10.20', average_price: '10.10', volume: '1000', diff: 0.1, dea: 0.05, macd: 0.1 }],
      })
      if (url === '/api/v1/replay/601872/evaluate' && init?.method === 'POST') return jsonResponse({
        symbol: '601872', trade_date: '20260831',
        markers: [{ index: 0, time: '09:31', price: 10.2, side: 'buy', rule_id: 'buy', rule_title: '买入' }],
        performance: { buy_count: 1, sell_count: 1, buy_cost: 1020, sell_proceeds: 1020, profit: 0, formula_return_pct: 0, forced_exit: { time: '09:31', price: 10.2 } },
      })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)
    render(<MarketApp />)

    await userEvent.click(await screen.findByRole('button', { name: '打开监控与复盘' }))
    expect(await screen.findByText('历史来源：TENCENT_PUBLIC')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '运行规则复盘' }))
    expect(await screen.findByText('B')).toBeInTheDocument()
    expect(screen.getByText(/收益率 0.00%/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '打开组合监控' }))
    expect(await screen.findByText('最新信号：买入')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '查看 QDII/LOF 溢价' }))
    expect(await screen.findByText('+20.00%')).toBeInTheDocument()
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
        source: 'TENCENT_PUBLIC',
        price_precision: 2,
        stale: false,
        age_seconds: 0.4,
        quote: { price: '8.33', previous_close: '8.16', change_percent: '2.08%', turnover_rate: '1.42%' },
        timeshare: [
          { time: '09:30', price: '8.20', average_price: null, volume: '1200' },
          { time: '10:00', price: null, average_price: null, volume: null },
          { time: '14:56', price: '8.33', average_price: null, volume: '900' },
        ],
        intraday_series: {
          large_order_net: {
            unit: null,
            points: [{ time: '09:30', value: '-0.03' }, { time: '14:56', value: '-0.02' }],
          },
          large_order_amount: {
            unit: '万',
            points: [{ time: '09:30', value: '-3397.0' }, { time: '14:56', value: '-2802.6' }],
          },
          retail_count: {
            unit: null,
            points: [{ time: '09:30', value: '21.75' }, { time: '14:56', value: '21.23' }],
          },
          macd_dif: {
            unit: null,
            points: [{ time: '09:30', value: '-0.001' }, { time: '14:56', value: '+0.002' }],
          },
          macd_dea: {
            unit: null,
            points: [{ time: '09:30', value: '-0.006' }, { time: '14:56', value: '-0.005' }],
          },
          macdfs: {
            unit: null,
            points: [{ time: '09:30', value: '+0.009' }, { time: '14:56', value: '+0.012' }],
          },
        },
        order_book: [],
        trades: [],
        main_fund_flow: {
          today: {
            unit: '万元',
            main_net_inflow: '-1983.50',
            main_visible_inflow: '-3711.70',
            main_hidden_inflow: '1728.20',
            retail_inflow: '1983.50',
          },
          three_day: {
            unit: '亿元',
            main_net_inflow: '-8.02',
            main_visible_inflow: '-6.99',
            main_hidden_inflow: '-1.03',
            retail_inflow: '8.02',
          },
          five_day: {
            unit: '亿元',
            main_net_inflow: '-14.63',
            main_visible_inflow: '-12.36',
            main_hidden_inflow: '-2.27',
            retail_inflow: '14.63',
          },
        },
        capabilities: {
          timeshare: { available: true, reason: null },
          kline: { available: false, reason: 'DIRECT_KLINE_UNAVAILABLE' },
          daily_kline: { available: true, adjustment: 'qfq' },
          l2: { available: true, reason: null },
          order_book: { available: false, reason: 'APP_INTERFACE_NOT_CONFIRMED' },
          trades: { available: false, reason: 'APP_INTERFACE_NOT_CONFIRMED' },
        },
        source_errors: { core_metrics: null, main_fund_flow: null },
      })
      if (url.includes('/series?period=day&limit=240')) return jsonResponse({
        symbol: '601872',
        period: 'day',
        bars: [
          { time: '2026-08-20', open: '8.00', high: '8.25', low: '7.95', close: '8.10', volume: '5000', amount: '40500' },
          { time: '2026-08-21', open: '8.10', high: '8.40', low: '8.00', close: '8.33', volume: '10000', amount: '83300' },
        ],
        indicators: {
          ma5: ['8.00', '8.20'], ma13: ['7.90', '8.10'], ma21: ['7.80', '8.00'], ma60: [null, null], ma120: [null, null], ma250: [null, null],
          boll_mid: ['8.00', '8.10'], boll_upper: ['8.30', '8.40'], boll_lower: ['7.70', '7.80'],
          macd_dif: ['0.09', '0.10'], macd_dea: ['0.07', '0.08'], macd_hist: ['0.03', '0.04'],
        },
        next_cursor: null,
        source_error: null,
        adjustment: 'qfq',
        source: 'THS_PUBLIC',
        cached: false,
        stale: false,
        source_errors: { ths_public_kline: null, tencent_public_kline: null },
      })
      if (url.includes('/fund-flow/history')) return jsonResponse({ symbol: '601872', name: '招商轮船', trade_date: null, available_dates: [], latest_sync_at: null, last_error: null, periods: { today: { points: [] }, three_day: { points: [] }, five_day: { points: [] } } })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    const watchlistButton = await screen.findByRole('button', { name: /招商轮船/ })
    expect(watchlistButton).toBeInTheDocument()
    expect(within(watchlistButton).getByText('招商轮船')).toHaveClass('market-symbol-name')
    expect((await screen.findAllByText('8.33')).length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('button', { name: '分时' }))
    const priceChart = screen.getByRole('img', { name: '招商轮船分时价格图' })
    const netChart = screen.getByRole('img', { name: '大单净量当日分时图' })
    const amountChart = screen.getByRole('img', { name: '大单金额当日分时图' })
    const retailChart = screen.getByRole('img', { name: '散户数量当日分时图' })
    const macdChart = screen.getByRole('img', { name: 'MACD当日分时图' })
    const unifiedChartPanel = priceChart.closest<HTMLElement>('.market-chart-panel')!
    const metricGrid = screen.getByText('大单净量', { selector: '.market-metric-grid span' }).closest<HTMLElement>('.market-metric-grid')!
    expect(unifiedChartPanel).toContainElement(metricGrid)
    expect(screen.queryByRole('heading', { name: '主力流向' })).not.toBeInTheDocument()
    expect(Number.parseFloat(getComputedStyle(unifiedChartPanel).marginTop)).toBe(0)
    expect(Number.parseFloat(getComputedStyle(metricGrid).marginTop)).toBe(0)
    expect(unifiedChartPanel).toContainElement(netChart)
    expect(unifiedChartPanel).toContainElement(amountChart)
    expect(unifiedChartPanel).toContainElement(retailChart)
    expect(unifiedChartPanel).toContainElement(macdChart)
    expect(unifiedChartPanel?.querySelector('.market-intraday-stack')).toBeNull()
    expect(screen.queryByRole('region', { name: 'App 内部指标分时' })).not.toBeInTheDocument()
    expect(screen.queryByText('APP INTERNAL')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '当日指标分时' })).not.toBeInTheDocument()
    expect(screen.queryByText('随上方分时十字线同步')).not.toBeInTheDocument()
    for (const chart of [netChart, amountChart, retailChart, macdChart]) {
      expect(within(chart.closest('figure')!).queryByText('当日分时')).not.toBeInTheDocument()
    }
    expect(priceChart.compareDocumentPosition(netChart) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(netChart.compareDocumentPosition(amountChart) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(amountChart.compareDocumentPosition(retailChart) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(retailChart.compareDocumentPosition(macdChart) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(macdChart.closest('figure')!).getByText('DIF')).toBeInTheDocument()
    expect(within(macdChart.closest('figure')!).getByText('DEA')).toBeInTheDocument()
    expect(within(macdChart.closest('figure')!).getByText('MACD', { selector: '.macd-readout b' })).toBeInTheDocument()
    expect(Number.parseFloat(getComputedStyle(priceChart).minHeight)).toBe(0)
    expect(Number.parseFloat(getComputedStyle(priceChart.closest('.market-timeshare-chart-frame')!).paddingTop)).toBe(0)
    expect(Number.parseFloat(getComputedStyle(priceChart.closest('.market-timeshare-chart-frame')!).paddingBottom)).toBe(0)
    for (const chart of [netChart, amountChart, retailChart, macdChart]) {
      const figure = chart.closest('figure')!
      const overlay = figure.querySelector('figcaption')!
      const watermark = figure.querySelector('h4')!
      const readout = figure.querySelector('.chart-readout')!
      const frame = figure.querySelector('.intraday-chart-frame')!
      expect(figure).toHaveClass('intraday-chart-compact')
      expect(overlay).toHaveClass('intraday-chart-overlay')
      expect(watermark).toHaveClass('intraday-chart-watermark')
      expect(readout).toHaveClass('intraday-chart-corner-readout')
      expect(getComputedStyle(watermark).opacity).toBe('0.6')
      expect(getComputedStyle(readout).opacity).toBe('0.6')
      expect(getComputedStyle(readout).right).toBe('3%')
      expect(getComputedStyle(overlay).zIndex).toBe('0')
      expect(getComputedStyle(frame).zIndex).toBe('1')
      expect(Number.parseFloat(getComputedStyle(figure).paddingBottom)).toBe(0)
      expect(chart).toHaveAttribute('viewBox', '0 0 720 128')
      expect(chart.querySelector('.chart-grid-line')).toHaveAttribute('y1', '8')
      expect(chart.querySelectorAll('.intraday-x-axis-label')).toHaveLength(0)
    }
    expect(within(priceChart).getByText('09:30')).toBeInTheDocument()
    expect(within(priceChart).getByText('11:30/13:00')).toBeInTheDocument()
    expect(within(priceChart).getByText('15:00')).toBeInTheDocument()
    expect(screen.getByText('14:56 更新')).toBeInTheDocument()
    expect(within(priceChart.closest('figure')!).getByText('14:56', { selector: 'time' })).toBeInTheDocument()
    expect(within(priceChart.closest('figure')!).getByText('8.33', { selector: '.market-timeshare-readout strong' })).toBeInTheDocument()

    priceChart.focus()
    await userEvent.keyboard('{ArrowLeft}')

    expect(within(priceChart.closest('figure')!).getByText('10:00', { selector: 'time' })).toBeInTheDocument()
    expect(within(priceChart.closest('figure')!).getByText('—', { selector: '.market-timeshare-readout strong' })).toBeInTheDocument()
    for (const chart of [netChart, amountChart, retailChart, macdChart]) {
      expect(within(chart.closest('figure')!).getByText('10:00', { selector: 'time' })).toBeInTheDocument()
    }
    expect(within(netChart.closest('figure')!).getByText('—', { selector: '.chart-readout strong' })).toBeInTheDocument()
    expect((priceChart.querySelector('.market-chart-line')?.getAttribute('d')?.match(/M/g) ?? [])).toHaveLength(2)

    await userEvent.keyboard('{Home}')

    expect(within(priceChart.closest('figure')!).getByText('09:30', { selector: 'time' })).toBeInTheDocument()
    expect(within(priceChart.closest('figure')!).getByText('8.20', { selector: '.market-timeshare-readout strong' })).toBeInTheDocument()
    for (const chart of [netChart, amountChart, retailChart, macdChart]) {
      expect(within(chart.closest('figure')!).getByText('09:30', { selector: 'time' })).toBeInTheDocument()
    }
    expect(within(netChart.closest('figure')!).getByText('-0.03', { selector: '.chart-readout strong' })).toBeInTheDocument()
    expect(within(amountChart.closest('figure')!).getByText('-3397.0万', { selector: '.chart-readout strong' })).toBeInTheDocument()
    expect(within(retailChart.closest('figure')!).getByText('21.75', { selector: '.chart-readout strong' })).toBeInTheDocument()
    expect(within(macdChart.closest('figure')!).getByText('-0.001', { selector: '.macd-readout strong' })).toBeInTheDocument()
    expect(priceChart.querySelector('.market-timeshare-cursor-x')).toBeInTheDocument()
    expect(priceChart.querySelector('.market-timeshare-cursor-y')).toBeInTheDocument()
    expect(macdChart.querySelector('.chart-cursor-line')).toBeInTheDocument()

    await userEvent.keyboard('{End}')
    Object.defineProperty(priceChart, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ x: 0, y: 0, left: 0, top: 0, right: 900, bottom: 360, width: 900, height: 360, toJSON: () => ({}) }),
    })
    fireEvent.pointerDown(priceChart, { clientX: 58, pointerId: 1, pointerType: 'touch' })
    expect(within(priceChart.closest('figure')!).getByText('09:30', { selector: 'time' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^(分时|日K|五日|周K|月K)$/ }).map((button) => button.textContent)).toEqual([
      '分时', '日K', '五日', '周K', '月K',
    ])
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/series?period=day')).length).toBe(0)
    await userEvent.click(await screen.findByRole('button', { name: '日K' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/market/symbols/601872/series?period=day&limit=240',
      expect.anything(),
    ))
    expect(await screen.findByRole('img', { name: '招商轮船前复权日K图' })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'MACD当日分时图' })).not.toBeInTheDocument()
    expect(screen.getByText('前复权')).toBeInTheDocument()
    expect(screen.getByText('10jqka 公开源')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '选择前一交易日' }))
    expect(screen.getByText('601872 · 2026-08-20 日K')).toBeInTheDocument()
    for (const [label, expected] of [
      ['开盘', '8.00'], ['最高', '8.25'], ['最低', '7.95'], ['收盘', '8.10'], ['成交量', '5000.00股'], ['成交额', '4.05万元'],
    ]) {
      const labelElement = screen.getByText(label, { selector: '.market-quote-strip span' })
      expect(within(labelElement.closest('div')!).getByText(expected, { selector: 'strong' })).toBeInTheDocument()
    }
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/series?period=day')).length).toBe(1)
  })

  it('deletes stocks from custom and primary lists while keeping every group synchronized', async () => {
    const shipping = { symbol: '601872', name: '招商轮船', market: '17' }
    const battery = { symbol: '300750', name: '宁德时代', market: '33' }
    let groups = [
      { id: 2, name: '航运', sort_order: 0, is_primary: false, items: [shipping] },
      { id: 1, name: '自选', sort_order: 1, is_primary: true, items: [shipping, battery] },
    ]
    const removedSymbols: string[] = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7,
        username: 'wilson',
        enabled: true,
        must_change_password: false,
        created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups })
      if (url.startsWith('/api/v1/watchlists/symbols/') && init?.method === 'DELETE') {
        const symbol = decodeURIComponent(url.split('/').at(-1)!)
        removedSymbols.push(symbol)
        groups = groups.map((group) => ({
          ...group,
          items: group.items.filter((item) => item.symbol !== symbol),
        }))
        return new Response(null, { status: 204 })
      }
      const snapshotMatch = url.match(/\/symbols\/(\d{6})\/snapshot/)
      if (snapshotMatch) {
        const item = [shipping, battery].find((candidate) => candidate.symbol === snapshotMatch[1])!
        return jsonResponse({
          symbol: item.symbol,
          name: item.name,
          market: item.market,
          sequence: 1,
          source_time: null,
          collected_at: '2026-08-25T01:30:00+00:00',
          stale: false,
          age_seconds: 0,
          quote: { price: '10.00', previous_close: '10.00', change_percent: '0.00%' },
          timeshare: [],
          intraday_series: {},
          order_book: [],
          trades: [],
          main_fund_flow: {},
          capabilities: {
            timeshare: { available: false },
            kline: { available: false },
            daily_kline: { available: true, adjustment: 'qfq' },
            order_book: { available: false },
            trades: { available: false },
          },
          source_errors: { core_metrics: null, main_fund_flow: null },
        })
      }
      const seriesMatch = url.match(/\/symbols\/(\d{6})\/series/)
      if (seriesMatch) return jsonResponse({
        symbol: seriesMatch[1],
        period: 'day',
        bars: [],
        indicators: {},
        next_cursor: null,
        source_error: 'KLINE_SOURCES_UNAVAILABLE',
        adjustment: 'qfq',
        source: null,
        cached: false,
        stale: false,
        source_errors: { public_kline: null, app_kline: null },
      })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)
    vi.stubGlobal('confirm', vi.fn(() => true))

    render(<MarketApp />)

    await screen.findByRole('button', { name: '新建自选分组' })
    await userEvent.click(screen.getByRole('button', { name: /^航运/ }))
    await userEvent.click(screen.getByRole('button', { name: /删除 601872.*航运和所有分组/ }))

    await waitFor(() => expect(removedSymbols).toEqual(['601872']))
    expect(screen.getByRole('button', { name: /^航运/ })).toHaveTextContent('0')
    expect(screen.getByRole('heading', { name: '宁德时代' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /^自选/ }))
    expect(screen.queryByRole('button', { name: /招商轮船 601872/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /宁德时代 300750/ })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /删除 300750.*自选和所有分组/ }))

    await waitFor(() => expect(removedSymbols).toEqual(['601872', '300750']))
    expect(screen.getByText('还没有自选股')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '从自选中选择一只股票' })).toBeInTheDocument()
  })

  it('keeps a slow previous daily response from replacing the selected stock', async () => {
    let resolveFirstSeries: (response: Response) => void = () => undefined
    let firstSeriesSignal: AbortSignal | null | undefined
    const firstSeries = new Promise<Response>((resolve) => { resolveFirstSeries = resolve })
    const seriesBody = (symbol: string) => ({
      symbol,
      period: 'day',
      bars: [{ time: '2026-08-21', open: '10', high: '11', low: '9', close: '10.5', volume: '10000', amount: '100000' }],
      indicators: {},
      next_cursor: null,
      source_error: null,
      adjustment: 'qfq',
      source: 'THS_PUBLIC',
      cached: false,
      stale: false,
      source_errors: { public_kline: null, app_kline: null },
    })
    const snapshotBody = (symbol: string, name: string) => ({
      symbol, name, market: '17', sequence: 1, source_time: '14:56',
      collected_at: '2026-08-23T06:56:01+00:00', stale: false, age_seconds: 0.2,
      quote: { price: '10.50' }, timeshare: [], intraday_series: {}, order_book: [], trades: [], main_fund_flow: {},
      capabilities: { kline: { available: false }, daily_kline: { available: true, adjustment: 'qfq' } },
      source_errors: { core_metrics: null, main_fund_flow: null },
    })
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input)
      if (url === '/api/v1/session') return Promise.resolve(jsonResponse({
        id: 7, username: 'wilson', enabled: true, must_change_password: false, created_at: '2026-08-23T00:00:00+00:00',
      }))
      if (url === '/api/v1/watchlists') return Promise.resolve(jsonResponse({ groups: [{
        id: 1, name: '自选', sort_order: 0, items: [
          { symbol: '601872', name: '招商轮船', market: '17' },
          { symbol: '600026', name: '中远海能', market: '17' },
        ],
      }] }))
      if (url.includes('/601872/snapshot')) return Promise.resolve(jsonResponse(snapshotBody('601872', '招商轮船')))
      if (url.includes('/600026/snapshot')) return Promise.resolve(jsonResponse(snapshotBody('600026', '中远海能')))
      if (url.includes('/601872/series')) {
        firstSeriesSignal = init?.signal
        return firstSeries
      }
      if (url.includes('/600026/series')) return Promise.resolve(jsonResponse(seriesBody('600026')))
      return Promise.reject(new Error(`unexpected request: ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    await userEvent.click(await screen.findByRole('button', { name: '日K' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/601872/series'))).toBe(true))
    await userEvent.click(await screen.findByRole('button', { name: /中远海能/ }))
    await userEvent.click(screen.getByRole('button', { name: '日K' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/600026/series'))).toBe(true))
    expect(await screen.findByRole('img', { name: '中远海能前复权日K图' })).toHaveTextContent('600026')
    expect(firstSeriesSignal?.aborted).toBe(true)

    resolveFirstSeries(jsonResponse(seriesBody('601872')))
    await waitFor(() => expect(screen.getByRole('img', { name: '中远海能前复权日K图' })).toHaveTextContent('600026'))
    expect(screen.queryByRole('img', { name: '招商轮船前复权日K图' })).not.toBeInTheDocument()
  })

  it('allows a transient daily K-line failure to be retried without reloading the page', async () => {
    let seriesCalls = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/session') return jsonResponse({
        id: 7, username: 'wilson', enabled: true, must_change_password: false, created_at: '2026-08-23T00:00:00+00:00',
      })
      if (url === '/api/v1/watchlists') return jsonResponse({ groups: [{
        id: 1, name: '自选', sort_order: 0, is_primary: true,
        items: [{ symbol: '601872', name: '招商轮船', market: '17' }],
      }] })
      if (url.includes('/snapshot')) return jsonResponse({
        symbol: '601872', name: '招商轮船', market: '17', sequence: 1, source_time: '15:00',
        collected_at: '2026-08-23T07:00:00+00:00', stale: false, age_seconds: 0.2,
        quote: { price: '19.78' }, timeshare: [], intraday_series: {}, order_book: [], trades: [], main_fund_flow: {},
        capabilities: { daily_kline: { available: true, adjustment: 'qfq' }, kline: { available: false } },
        source_errors: { core_metrics: null, main_fund_flow: null },
      })
      if (url.includes('/series?period=day&limit=240')) {
        seriesCalls += 1
        if (seriesCalls === 1) return jsonResponse({ detail: 'PUBLIC_KLINE_HTTP_ERROR' }, 503)
        return jsonResponse({
          symbol: '601872', period: 'day',
          bars: [{ time: '2026-08-21', open: '18.49', high: '20.10', low: '18.49', close: '19.78', volume: '193604931', amount: '3808373130' }],
          indicators: {}, next_cursor: null, source_error: null, adjustment: 'qfq', source: 'THS_PUBLIC', cached: false, stale: false,
          source_errors: { public_kline: null, app_kline: null },
        })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    await screen.findByRole('button', { name: /招商轮船/ })
    expect(seriesCalls).toBe(0)
    await userEvent.click(screen.getByRole('button', { name: '日K' }))
    await waitFor(() => expect(seriesCalls).toBe(1))
    expect(await screen.findByText('日 K 加载失败')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '重新加载日 K' }))

    expect(await screen.findByRole('img', { name: '招商轮船前复权日K图' })).toBeInTheDocument()
    expect(seriesCalls).toBe(2)
  })
})
