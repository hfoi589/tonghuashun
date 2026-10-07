import { useEffect, useMemo, useState } from 'react'
import { fundFlowSeriesConfig, fundFlowToWan, type FundFlowSeriesKey } from './FundFlowHistoryChart'
import { marketApi, type FundFlowDailyPoint, type FundFlowDailyResponse } from './market-api'

function dateLabel(value: string): string {
  return value.length === 8 ? `${value.slice(4, 6)}-${value.slice(6)}` : value
}

function pointValue(point: FundFlowDailyPoint, key: FundFlowSeriesKey): number | null {
  return fundFlowToWan(point[key], point.unit)
}

function formatWan(value: number | null): string {
  return value === null || !Number.isFinite(value) ? '—' : value.toFixed(2)
}

type CumulativePoint = Omit<FundFlowDailyPoint, FundFlowSeriesKey> & Record<FundFlowSeriesKey, number | null>

export function FundFlowDailyChart({ points, onSelectedPointChange }: {
  points: FundFlowDailyPoint[]
  onSelectedPointChange?: (point: FundFlowDailyPoint) => void
}) {
  const drawable = points.filter((point) => fundFlowSeriesConfig.some(({ key }) => pointValue(point, key) !== null))
  const cumulative = useMemo<CumulativePoint[]>(() => {
    const totals = Object.fromEntries(fundFlowSeriesConfig.map(({ key }) => [key, 0])) as Record<FundFlowSeriesKey, number>
    return drawable.map((point) => {
      const next = { ...point } as unknown as CumulativePoint
      fundFlowSeriesConfig.forEach(({ key }) => {
        const value = pointValue(point, key)
        if (value !== null) totals[key] += value
        next[key] = value === null && totals[key] === 0 ? null : totals[key]
      })
      return next
    })
  }, [drawable])
  const [selectedIndex, setSelectedIndex] = useState(Math.max(0, cumulative.length - 1))
  useEffect(() => {
    setSelectedIndex(Math.max(0, cumulative.length - 1))
  }, [cumulative.length, cumulative.at(-1)?.trade_date])
  const values = useMemo(
    () => fundFlowSeriesConfig.flatMap(({ key }) => cumulative.flatMap((point) => {
      const value = point[key]
      return value === null ? [] : [value]
    })),
    [cumulative],
  )
  const activeIndex = Math.min(selectedIndex, Math.max(0, cumulative.length - 1))
  const activeSourcePoint = drawable[activeIndex]
  const activeSourceKey = activeSourcePoint
    ? JSON.stringify([
      activeIndex,
      activeSourcePoint.trade_date,
      activeSourcePoint.time,
      activeSourcePoint.main_net_inflow,
      activeSourcePoint.main_visible_inflow,
      activeSourcePoint.main_hidden_inflow,
      activeSourcePoint.retail_inflow,
      activeSourcePoint.periods,
    ])
    : ''
  useEffect(() => {
    if (onSelectedPointChange && activeSourcePoint) onSelectedPointChange(activeSourcePoint)
  }, [activeSourceKey, onSelectedPointChange])
  if (!cumulative.length || !values.length) return <div className="market-empty-chart">暂无最近30日收盘资金数据</div>

  const width = 900
  const height = 420
  const pad = { top: 24, right: 18, bottom: 96, left: 62 }
  const plotWidth = width - pad.left - pad.right
  const plotHeight = height - pad.top - pad.bottom
  const labelStep = Math.max(1, Math.ceil((cumulative.length - 1) / 5))
  const dateLabelIndexes = cumulative.reduce<number[]>((indexes, _point, index) => {
    if (index === 0 || index === cumulative.length - 1 || index % labelStep === 0) indexes.push(index)
    return indexes
  }, [])
  const minValue = Math.min(0, ...values)
  const maxValue = Math.max(0, ...values)
  const spread = Math.max(0.01, maxValue - minValue)
  const min = minValue - spread * 0.08
  const max = maxValue + spread * 0.08
  const x = (index: number) => pad.left + index / Math.max(1, cumulative.length - 1) * plotWidth
  const y = (value: number) => pad.top + (max - value) / (max - min) * plotHeight
  const pathFor = (key: FundFlowSeriesKey) => cumulative.reduce((path, point, index) => {
    const value = point[key]
    if (value === null) return path
    return `${path}${path ? ' L' : 'M'}${x(index).toFixed(2)},${y(value).toFixed(2)}`
  }, '')
  const activePoint = cumulative[activeIndex]

  return <section className="fund-flow-daily-chart" aria-label="近30日收盘资金流向">
    <div className="fund-flow-daily-readout" aria-live="polite">
      <time>{activePoint.trade_date}</time>
      {fundFlowSeriesConfig.map(({ key, label, className }) => <span key={key}>
        <small>{label}</small><strong className={`fund-flow-daily-value-${className}`}>{formatWan(activePoint[key])}</strong>
      </span>)}
    </div>
    <svg
      className="fund-flow-daily-svg"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="最近30个交易日资金流向折线图"
      tabIndex={0}
      onPointerMove={(event) => {
        const rect = event.currentTarget.getBoundingClientRect()
        if (rect.width <= 0) return
        const svgX = (event.clientX - rect.left) / rect.width * width
        const ratio = Math.max(0, Math.min(1, (svgX - pad.left) / plotWidth))
        setSelectedIndex(Math.round(ratio * Math.max(0, cumulative.length - 1)))
      }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowLeft') setSelectedIndex((index) => Math.max(0, index - 1))
        if (event.key === 'ArrowRight') setSelectedIndex((index) => Math.min(cumulative.length - 1, index + 1))
      }}
    >
      {[0, 1, 2, 3, 4].map((tick) => {
        const value = max - tick / 4 * (max - min)
        const py = pad.top + tick / 4 * plotHeight
        return <g key={tick}><line x1={pad.left} x2={width - pad.right} y1={py} y2={py} /><text x={pad.left - 9} y={py + 4} textAnchor="end">{value.toFixed(2)}</text></g>
      })}
      <line className="fund-flow-history-zero" x1={pad.left} x2={width - pad.right} y1={y(0)} y2={y(0)} />
      {fundFlowSeriesConfig.map(({ key, className, color }) => <path key={key} className={`fund-flow-history-series fund-flow-history-series-${className}`} d={pathFor(key)} style={{ stroke: color }} />)}
      <line className="fund-flow-history-cursor" x1={x(activeIndex)} x2={x(activeIndex)} y1={pad.top} y2={height - pad.bottom} />
      {fundFlowSeriesConfig.map(({ key, className, color }) => {
        const value = activePoint[key]
        return value === null ? null : <circle key={key} className={`fund-flow-history-dot fund-flow-history-dot-${className}`} cx={x(activeIndex)} cy={y(value)} r="4" style={{ fill: color }} />
      })}
      {dateLabelIndexes.map((index) => {
        const point = cumulative[index]
        return <text className="fund-flow-daily-axis-label" key={`${point.trade_date}-${index}`} x={x(index)} y={height - 24} textAnchor="end" transform={`rotate(-55 ${x(index)} ${height - 24})`}>{dateLabel(point.trade_date)}</text>
      })}
    </svg>
  </section>
}

export function FundFlowDailyPanel({ symbol, pollingEnabled = true, onSelectedPointChange }: {
  symbol: string
  pollingEnabled?: boolean
  onSelectedPointChange?: (point: FundFlowDailyPoint) => void
}) {
  const [history, setHistory] = useState<FundFlowDailyResponse>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>()
  const [refreshVersion, setRefreshVersion] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(undefined)
    void marketApi.fundFlowDaily(symbol, 30, controller.signal)
      .then(setHistory)
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '资金日线读取失败') })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [refreshVersion, symbol])
  useEffect(() => {
    if (!pollingEnabled) return
    const timer = window.setInterval(() => setRefreshVersion((version) => version + 1), 30_000)
    return () => window.clearInterval(timer)
  }, [pollingEnabled])
  return <section className="fund-flow-daily-panel" aria-label="近30日收盘资金流向">
    <div className="market-section-title"><div><span>30D CAPITAL FLOW</span><h3>近30日资金流向</h3></div><small>每日收盘值 · 单位：万元</small></div>
    {loading ? <div className="market-empty-chart">正在加载资金日线…</div>
      : error ? <div className="market-capability-gap"><strong>资金日线加载失败</strong><span>{error}</span></div>
        : <FundFlowDailyChart points={history?.points ?? []} onSelectedPointChange={onSelectedPointChange} />}
  </section>
}
