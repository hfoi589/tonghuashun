import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import type { MarketSeriesPage } from './market-api'

const chartHarness = vi.hoisted(() => {
  const series: Array<{ kind: string, options: Record<string, unknown>, pane: number, setData: ReturnType<typeof vi.fn> }> = []
  const subscribeCrosshairMove = vi.fn()
  const createChart = vi.fn(() => ({
    addSeries: vi.fn((kind: string, options: Record<string, unknown>, pane = 0) => {
      const item = { kind, options, pane, setData: vi.fn() }
      series.push(item)
      return item
    }),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    panes: () => [{ setHeight: vi.fn() }, { setHeight: vi.fn() }, { setHeight: vi.fn() }],
    timeScale: () => ({ fitContent: vi.fn() }),
    subscribeCrosshairMove,
  }))
  return { series, createChart, subscribeCrosshairMove }
})

vi.mock('lightweight-charts', () => ({
  createChart: chartHarness.createChart,
  CandlestickSeries: 'CandlestickSeries',
  HistogramSeries: 'HistogramSeries',
  LineSeries: 'LineSeries',
  ColorType: { Solid: 'Solid' },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dashed: 2 },
}))

import { DailyKChart } from './DailyKChart'

const indicatorNames = [
  'ma5', 'ma13', 'ma21', 'ma60', 'ma120', 'ma250',
  'boll_mid', 'boll_upper', 'boll_lower',
  'macd_dif', 'macd_dea', 'macd_hist',
]

function page(): MarketSeriesPage {
  return {
    symbol: '601872',
    period: 'day',
    bars: [
      { time: '2026-08-20', open: '18.70', high: '19.00', low: '18.55', close: '18.66', volume: '5000', amount: '188000' },
      { time: '2026-08-21', open: '18.49', high: '20.10', low: '18.49', close: '19.78', volume: '193604931', amount: '3808373130' },
    ],
    indicators: Object.fromEntries(indicatorNames.map((name, index) => [name, [null, String(index + 1)]])),
    next_cursor: null,
    source_error: null,
    adjustment: 'qfq',
    source: 'THS_PUBLIC',
    cached: false,
    stale: false,
    source_errors: { public_kline: null, app_kline: null },
  }
}

afterEach(() => {
  cleanup()
  chartHarness.series.splice(0)
  chartHarness.createChart.mockClear()
  chartHarness.subscribeCrosshairMove.mockClear()
})

describe('DailyKChart', () => {
  it('places candles and nine overlays above volume and MACD panes', () => {
    render(<DailyKChart name="招商轮船" page={page()} />)

    expect(chartHarness.series).toHaveLength(14)
    expect(chartHarness.series.slice(0, 10).map((item) => item.pane)).toEqual(Array(10).fill(0))
    expect(chartHarness.series[10]).toMatchObject({ kind: 'HistogramSeries', pane: 1 })
    expect(chartHarness.series.slice(11).map((item) => item.pane)).toEqual([2, 2, 2])
    expect(chartHarness.series[0].setData).toHaveBeenCalledWith(expect.arrayContaining([
      expect.objectContaining({ time: '2026-08-21', open: 18.49, close: 19.78 }),
    ]))
    expect(chartHarness.series[10].setData).toHaveBeenCalledWith(expect.arrayContaining([
      expect.objectContaining({ time: '2026-08-21', value: 193604931, color: '#e2232e' }),
    ]))
    expect(chartHarness.series.slice(1, 7).map((item) => ({
      lineStyle: item.options.lineStyle,
      lineWidth: item.options.lineWidth,
    }))).toEqual(Array(6).fill({ lineStyle: 0, lineWidth: 2 }))
    expect(chartHarness.series.slice(7, 10).map((item) => ({
      lineStyle: item.options.lineStyle,
      lineWidth: item.options.lineWidth,
    }))).toEqual(Array(3).fill({ lineStyle: 2, lineWidth: 1 }))
  })

  it('shows aligned latest values and TradingView attribution', () => {
    render(<DailyKChart name="招商轮船" page={page()} />)

    expect(screen.getByText('2026-08-21')).toBeInTheDocument()
    expect(screen.queryByTestId('readout-ohlc')).not.toBeInTheDocument()
    expect(screen.getByTestId('readout-volume')).toHaveTextContent('成交量 1.94亿股')
    expect(screen.getByTestId('readout-ma5')).toHaveTextContent('MA5 1')
    expect(screen.getByTestId('readout-boll-mid')).toHaveTextContent('BOLL MID 7')
    expect(screen.getByTestId('readout-dif')).toHaveTextContent('DIF 10')
    expect(screen.getByRole('link', { name: /TradingView/ })).toHaveAttribute(
      'href',
      'https://www.tradingview.com/',
    )
    expect(chartHarness.subscribeCrosshairMove).toHaveBeenCalledOnce()
  })

  it('uses the plotted series colors in the selected-point readout', () => {
    const value = page()
    const onSelectionChange = vi.fn()
    value.indicators.ma5 = ['17.11', '18.54']
    value.indicators.macd_hist = ['-0.50', '0.50']
    render(<DailyKChart name="招商轮船" page={value} onSelectionChange={onSelectionChange} />)

    expect(screen.getByTestId('readout-ma5')).toHaveStyle({ color: '#2d3439' })
    expect(screen.getByTestId('readout-ma13')).toHaveStyle({ color: '#f28e2b' })
    expect(screen.getByTestId('readout-boll-mid')).toHaveStyle({ color: '#4b91e6' })
    expect(screen.getByTestId('readout-boll-upper')).toHaveStyle({ color: '#e2232e' })
    expect(screen.getByTestId('readout-boll-lower')).toHaveStyle({ color: '#159447' })
    expect(screen.getByTestId('readout-dif')).toHaveStyle({ color: '#30343a' })
    expect(screen.getByTestId('readout-dea')).toHaveStyle({ color: '#f06b35' })
    expect(screen.getByTestId('readout-volume')).toHaveStyle({ color: '#e2232e' })
    expect(screen.getByTestId('readout-macd')).toHaveStyle({ color: '#e2232e' })
    expect(screen.queryByTestId('readout-ohlc')).not.toBeInTheDocument()

    expect(chartHarness.series[1].options.color).toBe('#2d3439')
    expect(chartHarness.series[7].options.color).toBe('#4b91e6')
    expect(chartHarness.series[12].options.color).toBe('#30343a')
    expect(chartHarness.series[13].options.color).toBe('#f06b35')

    act(() => {
      chartHarness.subscribeCrosshairMove.mock.calls[0][0]({ time: '2026-08-20' })
    })

    expect(screen.getByText('2026-08-20')).toBeInTheDocument()
    expect(screen.getByTestId('readout-ma5')).toHaveTextContent('MA5 17.11')
    expect(screen.getByTestId('readout-volume')).toHaveStyle({ color: '#00966d' })
    expect(screen.getByTestId('readout-volume')).toHaveTextContent('成交量 5000.00股')
    expect(screen.getByTestId('readout-macd')).toHaveStyle({ color: '#00966d' })
    return waitFor(() => expect(onSelectionChange).toHaveBeenLastCalledWith(expect.objectContaining({
      index: 0,
      bar: expect.objectContaining({ time: '2026-08-20', open: '18.70', close: '18.66' }),
    })))
  })

  it('rounds every top indicator value to two decimals without repeating OHLC', () => {
    const value = page()
    value.indicators.ma5 = ['18.491', '18.542']
    value.indicators.boll_mid = ['17.201', '17.399']
    value.indicators.macd_dif = ['0.401', '0.646024']
    value.indicators.macd_dea = ['0.301', '0.428722']
    value.indicators.macd_hist = ['-0.201', '0.434604']

    render(<DailyKChart name="招商轮船" page={value} />)

    expect(screen.queryByTestId('readout-ohlc')).not.toBeInTheDocument()
    expect(screen.getByTestId('readout-ma5')).toHaveTextContent(/^MA5 18\.54$/)
    expect(screen.getByTestId('readout-boll-mid')).toHaveTextContent(/^BOLL MID 17\.40$/)
    expect(screen.getByTestId('readout-dif')).toHaveTextContent(/^DIF 0\.65$/)
    expect(screen.getByTestId('readout-dea')).toHaveTextContent(/^DEA 0\.43$/)
    expect(screen.getByTestId('readout-macd')).toHaveTextContent(/^MACD 0\.43$/)
  })
})
