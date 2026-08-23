import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { MarketSeriesPage } from './market-api'

const chartHarness = vi.hoisted(() => {
  const series: Array<{ kind: string, pane: number, setData: ReturnType<typeof vi.fn> }> = []
  const subscribeCrosshairMove = vi.fn()
  const createChart = vi.fn(() => ({
    addSeries: vi.fn((kind: string, _options: unknown, pane = 0) => {
      const item = { kind, pane, setData: vi.fn() }
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
      { time: '2026-08-20', open: '18.70', high: '19.00', low: '18.55', close: '18.66', volume: '10000', amount: '188000' },
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
  })

  it('shows aligned latest values and TradingView attribution', () => {
    render(<DailyKChart name="招商轮船" page={page()} />)

    expect(screen.getByText('2026-08-21')).toBeInTheDocument()
    expect(screen.getByText(/O 18.49/)).toBeInTheDocument()
    expect(screen.getByText(/1.94亿股/)).toBeInTheDocument()
    expect(screen.getByText(/MA5 1/)).toBeInTheDocument()
    expect(screen.getByText(/BOLL 7\/8\/9/)).toBeInTheDocument()
    expect(screen.getByText(/DIF 10/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /TradingView/ })).toHaveAttribute(
      'href',
      'https://www.tradingview.com/',
    )
    expect(chartHarness.subscribeCrosshairMove).toHaveBeenCalledOnce()
  })
})
