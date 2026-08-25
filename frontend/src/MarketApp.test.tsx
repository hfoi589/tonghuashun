import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MarketApp, nextIntradaySelectedTime } from './MarketApp'

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
  vi.restoreAllMocks()
})

describe('MarketApp', () => {
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
        main_fund_flow: {},
        capabilities: {
          timeshare: { available: true, reason: null },
          kline: { available: false, reason: 'DIRECT_KLINE_UNAVAILABLE' },
          daily_kline: { available: true, adjustment: 'qfq' },
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
        source_errors: { public_kline: null, app_kline: null },
      })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    const watchlistButton = await screen.findByRole('button', { name: /招商轮船/ })
    expect(watchlistButton).toBeInTheDocument()
    expect(within(watchlistButton).getByText('招商轮船')).toHaveClass('market-symbol-name')
    expect((await screen.findAllByText('8.33')).length).toBeGreaterThan(0)
    const priceChart = screen.getByRole('img', { name: '招商轮船分时价格图' })
    const netChart = screen.getByRole('img', { name: '大单净量当日分时图' })
    const amountChart = screen.getByRole('img', { name: '大单金额当日分时图' })
    const retailChart = screen.getByRole('img', { name: '散户数量当日分时图' })
    const macdChart = screen.getByRole('img', { name: 'MACD当日分时图' })
    const unifiedChartPanel = priceChart.closest('.market-chart-panel')
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
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/market/symbols/601872/series?period=day&limit=240',
      expect.anything(),
    ))
    await userEvent.click(screen.getByRole('button', { name: '日K' }))
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
      { id: 1, name: '自选', sort_order: 0, is_primary: true, items: [shipping, battery] },
      { id: 2, name: '航运', sort_order: 1, is_primary: false, items: [shipping] },
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
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
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
      if (url.includes('/601872/series')) return firstSeries
      if (url.includes('/600026/series')) return Promise.resolve(jsonResponse(seriesBody('600026')))
      return Promise.reject(new Error(`unexpected request: ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', undefined)

    render(<MarketApp />)

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/601872/series'))).toBe(true))
    await userEvent.click(await screen.findByRole('button', { name: /中远海能/ }))
    await userEvent.click(screen.getByRole('button', { name: '日K' }))
    expect(await screen.findByRole('img', { name: '中远海能前复权日K图' })).toHaveTextContent('600026')

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
    await waitFor(() => expect(seriesCalls).toBe(1))
    await userEvent.click(screen.getByRole('button', { name: '日K' }))
    expect(await screen.findByText('日 K 加载失败')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '重新加载日 K' }))

    expect(await screen.findByRole('img', { name: '招商轮船前复权日K图' })).toBeInTheDocument()
    expect(seriesCalls).toBe(2)
  })
})
