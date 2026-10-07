import { useEffect, useMemo, useState } from 'react'
import { intradayTimeRatio } from './intraday-axis'
import type { FundFlowHistoryPeriod } from './market-api'

export type { FundFlowHistoryPeriod } from './market-api'

export type FundFlowSeriesKey = 'retail_inflow' | 'main_visible_inflow' | 'main_hidden_inflow' | 'main_net_inflow'

export const fundFlowSeriesConfig: Array<{ key: FundFlowSeriesKey, label: string, className: string, color: string }> = [
  { key: 'retail_inflow', label: '散户流入', className: 'retail', color: '#16803c' },
  { key: 'main_visible_inflow', label: '主力明盘', className: 'visible', color: '#d92d20' },
  { key: 'main_hidden_inflow', label: '主力暗盘', className: 'hidden', color: '#7f1d1d' },
  { key: 'main_net_inflow', label: '主力净流入', className: 'net', color: '#b7791f' },
]

export function fundFlowToWan(value: string | null, unit: string | null): number | null {
  if (value === null || value === undefined || value === '') return null
  const numeric = Number(String(value).replaceAll(',', ''))
  if (!Number.isFinite(numeric)) return null
  if (unit === '万元' || unit === '万') return numeric
  if (unit === '亿元' || unit === '亿') return numeric * 10_000
  return null
}

// Retain the 亿元 helper for callers that need it; rendered chart values use 万元.
export function fundFlowToYi(value: string | null, unit: string | null): number | null {
  const wan = fundFlowToWan(value, unit)
  return wan === null ? null : wan / 10_000
}

function seriesValue(point: FundFlowHistoryPeriod['points'][number], key: FundFlowSeriesKey): number | null {
  return fundFlowToWan(point[key], point.unit)
}

function formatWan(value: number | null): string {
  return value === null || !Number.isFinite(value) ? '—' : value.toFixed(2)
}

export function FundFlowHistoryChart({ period }: { period: FundFlowHistoryPeriod }) {
  const points = period.points.filter((point) => intradayTimeRatio(point.time) !== null)
  const [selectedIndex, setSelectedIndex] = useState(Math.max(0, points.length - 1))
  useEffect(() => {
    setSelectedIndex(Math.max(0, points.length - 1))
  }, [points.length, points.at(-1)?.time])
  const width = 900
  const height = 320
  const pad = { top: 24, right: 18, bottom: 42, left: 62 }
  const plotWidth = width - pad.left - pad.right
  const plotHeight = height - pad.top - pad.bottom
  const values = useMemo(
    () => fundFlowSeriesConfig.flatMap(({ key }) => points.flatMap((point) => {
      const value = seriesValue(point, key)
      return value === null ? [] : [value]
    })),
    [points],
  )

  if (points.length === 0 || values.length === 0) {
    return <div className="market-empty-chart">暂无该周期资金流历史</div>
  }

  const minValue = Math.min(0, ...values)
  const maxValue = Math.max(0, ...values)
  const spread = Math.max(0.01, maxValue - minValue)
  const min = minValue - spread * 0.08
  const max = maxValue + spread * 0.08
  const x = (index: number) => {
    const ratio = intradayTimeRatio(points[index].time)
    return pad.left + (ratio ?? index / Math.max(1, points.length - 1)) * plotWidth
  }
  const y = (value: number) => pad.top + (max - value) / (max - min) * plotHeight
  const pathFor = (key: FundFlowSeriesKey) => {
    let path = ''
    let segmentOpen = false
    points.forEach((point, index) => {
      const value = seriesValue(point, key)
      if (value === null) {
        segmentOpen = false
        return
      }
      path += `${segmentOpen ? 'L' : 'M'}${x(index).toFixed(2)},${y(value).toFixed(2)} `
      segmentOpen = true
    })
    return path.trim()
  }
  const activeIndex = Math.min(selectedIndex, points.length - 1)
  const activePoint = points[activeIndex]

  return <section className="fund-flow-history-chart" aria-label="主力流向历史折线图">
    <div className="fund-flow-history-head">
      <div className="fund-flow-history-legend">
        {fundFlowSeriesConfig.map((series) => <span key={series.key}><i style={{ backgroundColor: series.color }} />{series.label}</span>)}
      </div>
      <time>{activePoint.time}</time>
    </div>
    <div className="fund-flow-history-readout" aria-live="polite">
      {fundFlowSeriesConfig.map(({ key, label }) => <span key={key} data-testid={`fund-flow-readout-${key.replace('_inflow', '').replaceAll('_', '-')}`}>
        <small>{label}</small><strong>{formatWan(seriesValue(activePoint, key))}</strong>
      </span>)}
    </div>
    <svg
      className="fund-flow-history-svg"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label="主力明盘、主力暗盘、散户流入和主力净流入历史曲线"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'ArrowLeft') setSelectedIndex((index) => Math.max(0, index - 1))
        if (event.key === 'ArrowRight') setSelectedIndex((index) => Math.min(points.length - 1, index + 1))
      }}
    >
      {[0, 1, 2, 3, 4].map((tick) => {
        const value = max - tick / 4 * (max - min)
        const py = pad.top + tick / 4 * plotHeight
        return <g key={tick}><line x1={pad.left} x2={width - pad.right} y1={py} y2={py} /><text x={pad.left - 9} y={py + 4} textAnchor="end">{value.toFixed(2)}</text></g>
      })}
      <line className="fund-flow-history-zero" x1={pad.left} x2={width - pad.right} y1={y(0)} y2={y(0)} />
      {fundFlowSeriesConfig.map(({ key, className, color }) => <path
        key={key}
        className={`fund-flow-history-series fund-flow-history-series-${className}`}
        d={pathFor(key)}
        style={{ stroke: color }}
      />)}
      <line className="fund-flow-history-cursor" x1={x(activeIndex)} x2={x(activeIndex)} y1={pad.top} y2={height - pad.bottom} />
      {fundFlowSeriesConfig.map(({ key, className, color }) => {
        const value = seriesValue(activePoint, key)
        return value === null ? null : <circle key={key} className={`fund-flow-history-dot fund-flow-history-dot-${className}`} cx={x(activeIndex)} cy={y(value)} r="4" style={{ fill: color }} />
      })}
      <text x={pad.left} y={height - 12}>{points[0].time}</text>
      <text x={width - pad.right} y={height - 12} textAnchor="end">{points.at(-1)?.time}</text>
    </svg>
    <input
      className="fund-flow-history-scrubber"
      type="range"
      min={0}
      max={Math.max(0, points.length - 1)}
      value={activeIndex}
      onChange={(event) => setSelectedIndex(Number(event.target.value))}
      aria-label="选择资金流时间点"
      aria-valuetext={`${activePoint.time}，${fundFlowSeriesConfig.map(({ key, label }) => `${label}${formatWan(seriesValue(activePoint, key))}万元`).join('，')}`}
    />
  </section>
}
