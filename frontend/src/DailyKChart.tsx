import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type Time,
} from 'lightweight-charts'
import type { KlineBar, MarketSeriesPage } from './market-api'

export interface DailyKSelection {
  index: number
  bar: KlineBar
}

const UP_COLOR = '#e2232e'
const DOWN_COLOR = '#00966d'
const NEUTRAL_READOUT_COLOR = '#5d626a'

const movingAverageLines = [
  ['ma5', '#2d3439', 'MA5'],
  ['ma13', '#f28e2b', 'MA13'],
  ['ma21', '#7251a2', 'MA21'],
  ['ma60', '#2787d9', 'MA60'],
  ['ma120', '#b45fb2', 'MA120'],
  ['ma250', '#3f9d43', 'MA250'],
] as const

const bollLines = [
  ['boll_mid', '#4b91e6', 'BOLL MID'],
  ['boll_upper', '#e2232e', 'BOLL UPPER'],
  ['boll_lower', '#159447', 'BOLL LOWER'],
] as const

const overlayLines = [...movingAverageLines, ...bollLines] as const
const macdLines = [
  ['macd_dif', '#30343a', 'DIF', 'readout-dif'],
  ['macd_dea', '#f06b35', 'DEA', 'readout-dea'],
] as const

function number(value: string | null | undefined): number | null {
  const result = Number(value)
  return value === null || value === undefined || value === '' || !Number.isFinite(result)
    ? null
    : result
}

function volumeLabel(value: string | null | undefined): string {
  const result = number(value)
  if (result === null) return '—'
  if (Math.abs(result) >= 100_000_000) return `${(result / 100_000_000).toFixed(2)}亿股`
  if (Math.abs(result) >= 10_000) return `${(result / 10_000).toFixed(2)}万股`
  return `${result.toFixed(2)}股`
}

function display(value: string | null | undefined): string {
  const result = number(value)
  return result === null ? '—' : result.toFixed(2)
}

function lineData(page: MarketSeriesPage, name: string) {
  return page.bars.flatMap((bar, index) => {
    const value = number(page.indicators[name]?.[index])
    return value === null ? [] : [{ time: bar.time as Time, value }]
  })
}

function barColor(bar: KlineBar): string {
  const open = number(bar.open)
  const close = number(bar.close)
  if (open === null || close === null) return NEUTRAL_READOUT_COLOR
  return close >= open ? UP_COLOR : DOWN_COLOR
}

function histogramColor(value: string | null | undefined): string {
  const current = number(value)
  if (current === null) return NEUTRAL_READOUT_COLOR
  return current >= 0 ? UP_COLOR : DOWN_COLOR
}

function ReadoutMetric({ testId, label, value, color }: {
  testId: string,
  label: string,
  value: string | null | undefined,
  color: string,
}) {
  return <span className="daily-k-readout-metric" data-testid={testId} style={{ color }}>
    <b>{label}</b>{' '}<span>{display(value)}</span>
  </span>
}

function readout(page: MarketSeriesPage, selectedTime: string | undefined) {
  const index = Math.max(0, page.bars.findIndex((bar) => bar.time === selectedTime))
  const fallback = page.bars.length - 1
  const selectedIndex = index === 0 && page.bars[0]?.time !== selectedTime ? fallback : index
  return {
    index: selectedIndex,
    bar: page.bars[selectedIndex],
    indicator: (name: string) => page.indicators[name]?.[selectedIndex],
  }
}

export function DailyKChart({ name, page, onSelectionChange }: {
  name: string,
  page: MarketSeriesPage,
  onSelectionChange?: (selection: DailyKSelection) => void,
}) {
  const container = useRef<HTMLDivElement>(null)
  const latestTime = page.bars.at(-1)?.time
  const [selectedTime, setSelectedTime] = useState<string | undefined>(latestTime)
  const selected = useMemo(() => readout(page, selectedTime), [page, selectedTime])

  useEffect(() => setSelectedTime(latestTime), [latestTime])

  useEffect(() => {
    if (selected.bar) onSelectionChange?.({ index: selected.index, bar: selected.bar })
  }, [onSelectionChange, selected.bar, selected.index])

  useEffect(() => {
    if (!container.current || page.bars.length === 0) return
    const chart = createChart(container.current, {
      autoSize: true,
      height: 720,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#747981',
        attributionLogo: true,
        panes: { separatorColor: '#e7e8eb', separatorHoverColor: '#d5d8dd' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      grid: {
        vertLines: { color: '#f0f1f3' },
        horzLines: { color: '#eceef1' },
      },
      rightPriceScale: { borderColor: '#e2e4e7' },
      timeScale: { borderColor: '#e2e4e7', timeVisible: false },
    })

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: UP_COLOR,
      downColor: DOWN_COLOR,
      borderUpColor: UP_COLOR,
      borderDownColor: DOWN_COLOR,
      wickUpColor: UP_COLOR,
      wickDownColor: DOWN_COLOR,
    }, 0)
    candles.setData(page.bars.flatMap((bar) => {
      const open = number(bar.open)
      const high = number(bar.high)
      const low = number(bar.low)
      const close = number(bar.close)
      return open === null || high === null || low === null || close === null
        ? []
        : [{ time: bar.time as Time, open, high, low, close }]
    }))

    overlayLines.forEach(([key, color]) => {
      const series = chart.addSeries(LineSeries, {
        color,
        lineWidth: 1,
        lineStyle: key.startsWith('boll_') ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      }, 0)
      series.setData(lineData(page, key))
    })

    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceLineVisible: false,
      lastValueVisible: false,
    }, 1)
    volume.setData(page.bars.flatMap((bar) => {
      const value = number(bar.volume)
      return value === null ? [] : [{
        time: bar.time as Time,
        value,
        color: barColor(bar),
      }]
    }))

    const macdHistogram = chart.addSeries(HistogramSeries, {
      priceLineVisible: false,
      lastValueVisible: false,
    }, 2)
    macdHistogram.setData(lineData(page, 'macd_hist').map((point) => ({
      ...point,
      color: point.value >= 0 ? UP_COLOR : DOWN_COLOR,
    })))
    const dif = chart.addSeries(LineSeries, {
      color: macdLines[0][1], lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    }, 2)
    dif.setData(lineData(page, 'macd_dif'))
    const dea = chart.addSeries(LineSeries, {
      color: macdLines[1][1], lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    }, 2)
    dea.setData(lineData(page, 'macd_dea'))

    chart.panes()[0]?.setHeight(420)
    chart.panes()[1]?.setHeight(140)
    chart.panes()[2]?.setHeight(160)
    chart.timeScale().fitContent()
    chart.subscribeCrosshairMove((parameter) => {
      if (parameter.time === undefined) {
        setSelectedTime(latestTime)
      } else {
        setSelectedTime(String(parameter.time))
      }
    })
    return () => chart.remove()
  }, [latestTime, name, page])

  if (!selected.bar) return <div className="market-empty-chart">暂无日 K 数据</div>
  const bar: KlineBar = selected.bar
  return <div className="daily-k-chart-shell">
    <div className="daily-k-readout" aria-live="polite">
      <div className="daily-k-readout-row daily-k-readout-primary">
        <time>{bar.time}</time>
        <span className="daily-k-readout-metric" data-testid="readout-volume" style={{ color: barColor(bar) }}>
          <b>成交量</b>{' '}<span>{volumeLabel(bar.volume)}</span>
        </span>
      </div>
      <div className="daily-k-readout-row" aria-label="均线指标">
        {movingAverageLines.map(([key, color, label]) => <ReadoutMetric
          color={color}
          key={key}
          label={label}
          testId={`readout-${key}`}
          value={selected.indicator(key)}
        />)}
      </div>
      <div className="daily-k-readout-row" aria-label="布林线指标">
        {bollLines.map(([key, color, label]) => <ReadoutMetric
          color={color}
          key={key}
          label={label}
          testId={`readout-${key.replace('_', '-')}`}
          value={selected.indicator(key)}
        />)}
      </div>
      <div className="daily-k-readout-row" aria-label="MACD 指标">
        {macdLines.map(([key, color, label, testId]) => <ReadoutMetric
          color={color}
          key={key}
          label={label}
          testId={testId}
          value={selected.indicator(key)}
        />)}
        <ReadoutMetric
          color={histogramColor(selected.indicator('macd_hist'))}
          label="MACD"
          testId="readout-macd"
          value={selected.indicator('macd_hist')}
        />
      </div>
    </div>
    <div ref={container} className="daily-k-chart" role="img" aria-label={`${name}前复权日K图`} />
    <p className="daily-k-attribution">
      图表由 <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView Lightweight Charts</a> 提供
    </p>
  </div>
}
