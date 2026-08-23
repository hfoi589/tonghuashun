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

const overlayLines = [
  ['ma5', '#2d3439', 'MA5'],
  ['ma13', '#f28e2b', 'MA13'],
  ['ma21', '#7251a2', 'MA21'],
  ['ma60', '#2787d9', 'MA60'],
  ['ma120', '#b45fb2', 'MA120'],
  ['ma250', '#3f9d43', 'MA250'],
  ['boll_mid', '#4b91e6', 'BOLL MID'],
  ['boll_upper', '#e2232e', 'BOLL UPPER'],
  ['boll_lower', '#159447', 'BOLL LOWER'],
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
  return `${result.toFixed(0)}股`
}

function display(value: string | null | undefined): string {
  return value === null || value === undefined || value === '' ? '—' : value
}

function lineData(page: MarketSeriesPage, name: string) {
  return page.bars.flatMap((bar, index) => {
    const value = number(page.indicators[name]?.[index])
    return value === null ? [] : [{ time: bar.time as Time, value }]
  })
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

export function DailyKChart({ name, page }: { name: string, page: MarketSeriesPage }) {
  const container = useRef<HTMLDivElement>(null)
  const latestTime = page.bars.at(-1)?.time
  const [selectedTime, setSelectedTime] = useState<string | undefined>(latestTime)
  const selected = useMemo(() => readout(page, selectedTime), [page, selectedTime])

  useEffect(() => setSelectedTime(latestTime), [latestTime])

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
      upColor: '#e2232e',
      downColor: '#00966d',
      borderUpColor: '#e2232e',
      borderDownColor: '#00966d',
      wickUpColor: '#e2232e',
      wickDownColor: '#00966d',
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
      const open = number(bar.open)
      const close = number(bar.close)
      return value === null ? [] : [{
        time: bar.time as Time,
        value,
        color: close !== null && open !== null && close >= open ? '#e2232e' : '#00966d',
      }]
    }))

    const macdHistogram = chart.addSeries(HistogramSeries, {
      priceLineVisible: false,
      lastValueVisible: false,
    }, 2)
    macdHistogram.setData(lineData(page, 'macd_hist').map((point) => ({
      ...point,
      color: point.value >= 0 ? '#e2232e' : '#00966d',
    })))
    const dif = chart.addSeries(LineSeries, {
      color: '#30343a', lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    }, 2)
    dif.setData(lineData(page, 'macd_dif'))
    const dea = chart.addSeries(LineSeries, {
      color: '#f06b35', lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
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
      <strong>{bar.time}</strong>
      <span>O {display(bar.open)} H {display(bar.high)} L {display(bar.low)} C {display(bar.close)}</span>
      <span>成交量 {volumeLabel(bar.volume)}</span>
      <span>{overlayLines.slice(0, 6).map(([key, , label]) => `${label} ${display(selected.indicator(key))}`).join(' · ')}</span>
      <span>BOLL {display(selected.indicator('boll_mid'))}/{display(selected.indicator('boll_upper'))}/{display(selected.indicator('boll_lower'))}</span>
      <span>DIF {display(selected.indicator('macd_dif'))} · DEA {display(selected.indicator('macd_dea'))} · MACD {display(selected.indicator('macd_hist'))}</span>
    </div>
    <div ref={container} className="daily-k-chart" role="img" aria-label={`${name}前复权日K图`} />
    <p className="daily-k-attribution">
      图表由 <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView Lightweight Charts</a> 提供
    </p>
  </div>
}
