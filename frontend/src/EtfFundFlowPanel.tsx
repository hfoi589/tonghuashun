import { useEffect, useId, useMemo, useState } from 'react'
import type { EtfFlowPeriod, EtfFundFlowResponse, EtfHoldingItem } from './api'
import { isUnsupportedFundSymbol } from './FundFlowHistoryPanel'

export const etfPeriodOptions: Array<{ value: EtfFlowPeriod, label: string }> = [
  { value: '20d', label: '近20日' },
  { value: '60d', label: '近60日' },
  { value: '120d', label: '近120日' },
  { value: '250d', label: '近250日' },
]

type HoldingSortKey = 'net_inflow_raw' | 'streak_days' | 'hold_rate'
type SortOrder = 'asc' | 'desc'

function paddedRange(values: number[], fallbackMin = 0, fallbackMax = 1): [number, number] {
  if (!values.length) return [fallbackMin, fallbackMax]
  let min = Math.min(...values)
  let max = Math.max(...values)
  if (min === max) {
    const pad = Math.max(0.1, Math.abs(min) * 0.1)
    return [min - pad, max + pad]
  }
  const spread = max - min
  return [min - spread * 0.1, max + spread * 0.1]
}

function barRange(values: number[]): [number, number] {
  if (!values.length) return [-1, 1]
  let min = Math.min(0, ...values)
  let max = Math.max(0, ...values)
  if (min === max) return [-1, 1]
  const spread = max - min
  if (min < 0) min -= spread * 0.05
  if (max > 0) max += spread * 0.05
  return [min, max]
}

function emptyReason(symbol: string, market?: string): string {
  return isUnsupportedFundSymbol(symbol, market)
    ? '交易所基金（ETF/LOF）无个股关联 ETF 申购净流入统计。'
    : '暂无关联 ETF 申购净流入数据。'
}

function formatEtfFootnote(data: EtfFundFlowResponse): string {
  if (data.stale) {
    return `当前展示本地缓存兜底数据${data.data_date ? `（截至 ${data.data_date}）` : ''} · 通常于次日9点更新`
  }
  if (data.update_status === 'pending_morning_update') {
    return `上一交易日数据更新中（通常于交易日 09:00 左右更新${data.data_date ? `，当前截至 ${data.data_date}` : ''}）`
  }
  if (data.data_date) {
    return `数据截至 ${data.data_date} · 通常于次日9点更新`
  }
  return '通常于次日9点更新'
}

export function EtfFundFlowChartPanel({
  symbol,
  market,
  data,
  period,
  loading,
  error,
  onPeriodChange,
  onRetry,
  onOpenHoldings,
}: {
  symbol: string
  market?: string
  data?: EtfFundFlowResponse
  period: EtfFlowPeriod
  loading: boolean
  error?: string
  onPeriodChange: (period: EtfFlowPeriod) => void
  onRetry: () => void
  onOpenHoldings?: () => void
}) {
  const gradientId = useId().replaceAll(':', '')
  const points = data?.chart.points ?? []
  const [selectedIndex, setSelectedIndex] = useState(Math.max(0, points.length - 1))

  useEffect(() => {
    setSelectedIndex(Math.max(0, points.length - 1))
  }, [points.length, points.at(-1)?.date, period, symbol])

  const cumulativeValues = useMemo(
    () => points.flatMap((point) => (point.cumulative_value === null ? [] : [point.cumulative_value])),
    [points],
  )
  const priceValues = useMemo(
    () => points.flatMap((point) => (point.close_price === null ? [] : [point.close_price])),
    [points],
  )
  const dailyValues = useMemo(
    () => points.flatMap((point) => (point.daily_value === null ? [] : [point.daily_value])),
    [points],
  )

  if (loading && !data) {
    return <section className="etf-flow-panel" aria-label="ETF资金">
      <div className="etf-flow-head"><h3>ETF资金</h3></div>
      <div className="market-empty-chart">正在加载 ETF 资金数据…</div>
    </section>
  }

  if (error && !data) {
    return <section className="etf-flow-panel" aria-label="ETF资金">
      <div className="etf-flow-head"><h3>ETF资金</h3></div>
      <div className="market-capability-gap">
        <strong>ETF资金加载失败</strong>
        <span>{error}</span>
        <button type="button" className="inline-retry" onClick={onRetry}>重新加载</button>
      </div>
    </section>
  }

  const hasSummaryValues = data?.summary.metrics.some((metric) => metric.value_text !== '--') ?? false

  if (data && data.available && !points.length && !hasSummaryValues && (data.source_errors?.line || data.source_errors?.summary)) {
    return <section className="etf-flow-panel" aria-label="ETF资金">
      <div className="etf-flow-head">
        <h3>ETF资金</h3>
        {onOpenHoldings && <button type="button" className="etf-flow-more-link" onClick={onOpenHoldings}>明细 &gt;</button>}
      </div>
      <div className="market-capability-gap">
        <strong>ETF资金曲线暂不可用</strong>
        <span>上游走势接口暂时超时，可重新加载或先查看申购明细</span>
        <button type="button" className="inline-retry" onClick={onRetry}>重新加载</button>
      </div>
    </section>
  }

  if (!data || !data.available || (!points.length && !data.summary.metrics.length)) {
    return <section className="etf-flow-panel" aria-label="ETF资金">
      <div className="etf-flow-head"><h3>ETF资金</h3></div>
      <p className="etf-flow-empty">{emptyReason(symbol, market)}</p>
    </section>
  }

  const width = 900
  const height = 430
  const pad = { left: 56, right: 56 }
  const topPlot = { top: 18, bottom: 238 }
  const bottomPlot = { top: 304, bottom: 406 }
  const plotWidth = width - pad.left - pad.right
  const topHeight = topPlot.bottom - topPlot.top
  const bottomHeight = bottomPlot.bottom - bottomPlot.top

  const [cumMin, cumMax] = paddedRange(cumulativeValues, -1, 1)
  const [priceMin, priceMax] = paddedRange(priceValues, 0, 10)
  const [dayMin, dayMax] = barRange(dailyValues)

  const x = (index: number) => (
    points.length <= 1
      ? pad.left + plotWidth / 2
      : pad.left + (index / (points.length - 1)) * plotWidth
  )
  const yCum = (value: number) => topPlot.top + ((cumMax - value) / Math.max(1e-6, cumMax - cumMin)) * topHeight
  const yPrice = (value: number) => topPlot.top + ((priceMax - value) / Math.max(1e-6, priceMax - priceMin)) * topHeight
  const yBar = (value: number) => bottomPlot.top + ((dayMax - value) / Math.max(1e-6, dayMax - dayMin)) * bottomHeight

  const buildPath = (getter: (point: (typeof points)[number]) => number | null, yMapper: (value: number) => number) => {
    let path = ''
    let open = false
    points.forEach((point, index) => {
      const val = getter(point)
      if (val === null) {
        open = false
        return
      }
      path += `${open ? ' L' : 'M'}${x(index).toFixed(2)},${yMapper(val).toFixed(2)}`
      open = true
    })
    return path
  }

  const cumPath = buildPath((point) => point.cumulative_value, yCum)
  const pricePath = buildPath((point) => point.close_price, yPrice)
  const cumValidIndexes = points.flatMap((point, index) => (point.cumulative_value === null ? [] : [index]))
  const cumAreaPath = cumPath && cumValidIndexes.length >= 2
    ? `${cumPath} L${x(cumValidIndexes.at(-1)!).toFixed(2)},${topPlot.bottom} L${x(cumValidIndexes[0]).toFixed(2)},${topPlot.bottom} Z`
    : ''

  const activeIndex = points.length ? Math.min(selectedIndex, points.length - 1) : 0
  const activePoint = points[activeIndex]
  const barStep = points.length ? plotWidth / Math.max(1, points.length) : plotWidth
  const barWidth = Math.max(2, Math.min(12, barStep * 0.62))
  const zeroBarY = yBar(0)

  const xTicks = points.length === 0
    ? []
    : points.length === 1
      ? [{ index: 0, label: points[0].short_date, anchor: 'middle' as const }]
      : [
        { index: 0, label: points[0].short_date, anchor: 'start' as const },
        { index: Math.round((points.length - 1) / 2), label: points[Math.round((points.length - 1) / 2)].short_date, anchor: 'middle' as const },
        { index: points.length - 1, label: points.at(-1)!.short_date, anchor: 'end' as const },
      ]

  const selectAtClientX = (element: SVGSVGElement, clientX: number) => {
    if (!points.length) return
    const rect = element.getBoundingClientRect()
    if (rect.width <= 0) return
    const svgX = ((clientX - rect.left) / rect.width) * width
    const ratio = Math.max(0, Math.min(1, (svgX - pad.left) / plotWidth))
    setSelectedIndex(Math.round(ratio * Math.max(0, points.length - 1)))
  }

  const badge = data.summary.latest_badge
  const badgeArrow = badge?.tone === 'up' ? ' ↑' : badge?.tone === 'down' ? ' ↓' : ''

  return <section className="etf-flow-panel" aria-label="ETF资金">
    <div className="etf-flow-head">
      <div className="etf-flow-title-group">
        <h3>ETF资金</h3>
        {badge && <span className={`etf-flow-badge etf-tone-${badge.tone}`}>{badge.text}{badgeArrow}</span>}
      </div>
      {onOpenHoldings && <button type="button" className="etf-flow-more-link" onClick={onOpenHoldings}>明细 &gt;</button>}
    </div>

    {data.summary.metrics.length > 0 && <div className="etf-flow-summary-bar">
      {data.summary.metrics.map((metric) => <div key={metric.key} className="etf-flow-summary-item">
        <span>{metric.label}</span>
        <strong className={`etf-tone-${metric.tone}`}>{metric.value_text}</strong>
      </div>)}
    </div>}

    <div className="etf-flow-controls">
      <div className="etf-flow-legend">
        <span><i className="etf-legend-cum" />累计申购净流入({data.chart.cumulative_unit})</span>
        <span><i className="etf-legend-price" />收盘价</span>
      </div>
      <select
        className="etf-flow-period-select"
        aria-label="选择ETF资金区间"
        value={period}
        disabled={loading}
        onChange={(event) => onPeriodChange(event.target.value as EtfFlowPeriod)}
      >
        {etfPeriodOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
    </div>

    {loading && <p className="etf-flow-loading-tag">正在切换区间…</p>}
    {error && <p className="etf-flow-inline-warn">{error} <button type="button" className="inline-retry" onClick={onRetry}>重试</button></p>}
    {!error && data.stale && <p className="etf-flow-status-note">当前上游接口波动，已启用本地缓存兜底 <button type="button" className="inline-retry" onClick={onRetry}>刷新</button></p>}
    {!points.length && data.source_errors?.line && <div className="market-capability-gap">
      <strong>ETF资金曲线暂不可用</strong>
      <span>上游走势接口暂时超时，可重新加载或先查看申购明细</span>
      <button type="button" className="inline-retry" onClick={onRetry}>重新加载</button>
    </div>}

    {points.length > 0 && activePoint && <>
      <div className="etf-flow-readout" aria-live="polite">
        <time>{activePoint.date}</time>
        <span><small>累计申购</small><strong className={`etf-tone-${(activePoint.cumulative_value ?? 0) > 0 ? 'up' : (activePoint.cumulative_value ?? 0) < 0 ? 'down' : 'flat'}`}>{activePoint.cumulative_text ?? '--'}</strong></span>
        <span><small>收盘价</small><strong>{activePoint.close_price !== null ? activePoint.close_price.toFixed(2) : '--'}</strong></span>
        <span><small>当日申购</small><strong className={`etf-tone-${(activePoint.daily_value ?? 0) > 0 ? 'up' : (activePoint.daily_value ?? 0) < 0 ? 'down' : 'flat'}`}>{activePoint.daily_text ?? '--'}</strong></span>
      </div>

      <svg
        className="etf-flow-svg"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="ETF累计申购净流入与收盘价双轴走势图"
        tabIndex={0}
        onPointerMove={(event) => selectAtClientX(event.currentTarget, event.clientX)}
        onPointerDown={(event) => selectAtClientX(event.currentTarget, event.clientX)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowLeft') setSelectedIndex((idx) => Math.max(0, idx - 1))
          if (event.key === 'ArrowRight') setSelectedIndex((idx) => Math.min(points.length - 1, idx + 1))
        }}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#3366FF" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#3366FF" stopOpacity="0.01" />
          </linearGradient>
        </defs>

        {[0, 1, 2, 3, 4].map((tick) => {
          const py = topPlot.top + (tick / 4) * topHeight
          const leftVal = cumMax - (tick / 4) * (cumMax - cumMin)
          const rightVal = priceMax - (tick / 4) * (priceMax - priceMin)
          return <g key={tick}>
            <line className="etf-flow-grid" x1={pad.left} x2={width - pad.right} y1={py} y2={py} />
            <text className="etf-flow-axis-text" x={pad.left - 8} y={py + 4} textAnchor="end">{leftVal.toFixed(2)}</text>
            {priceValues.length > 0 && <text className="etf-flow-axis-text" x={width - pad.right + 8} y={py + 4} textAnchor="start">{rightVal.toFixed(2)}</text>}
          </g>
        })}

        {cumAreaPath && <path d={cumAreaPath} fill={`url(#${gradientId})`} />}
        {pricePath && <path className="etf-flow-price-line" d={pricePath} />}
        {cumPath && <path className="etf-flow-cum-line" d={cumPath} />}

        <line className="etf-flow-cursor" x1={x(activeIndex)} x2={x(activeIndex)} y1={topPlot.top} y2={topPlot.bottom} />
        {activePoint.cumulative_value !== null && <circle className="etf-flow-dot-cum" cx={x(activeIndex)} cy={yCum(activePoint.cumulative_value)} r="4" />}
        {activePoint.close_price !== null && <circle className="etf-flow-dot-price" cx={x(activeIndex)} cy={yPrice(activePoint.close_price)} r="4" />}

        {xTicks.map((tick) => <text key={`${tick.index}-${tick.label}`} className="etf-flow-axis-text" x={x(tick.index)} y={topPlot.bottom + 20} textAnchor={tick.anchor}>{tick.label}</text>)}

        <g transform={`translate(${pad.left}, 288)`}>
          <rect x="0" y="-9" width="7" height="10" rx="1.5" fill="#FA3B32" />
          <rect x="8" y="-6" width="7" height="7" rx="1.5" fill="#00B650" />
          <text className="etf-flow-bar-title" x="21" y="0">申购净流入({data.chart.daily_unit})</text>
        </g>

        <line className="etf-flow-grid" x1={pad.left} x2={width - pad.right} y1={bottomPlot.top} y2={bottomPlot.top} />
        <line className="etf-flow-zero" x1={pad.left} x2={width - pad.right} y1={zeroBarY} y2={zeroBarY} />
        <line className="etf-flow-grid" x1={pad.left} x2={width - pad.right} y1={bottomPlot.bottom} y2={bottomPlot.bottom} />
        <text className="etf-flow-axis-text" x={pad.left - 8} y={bottomPlot.top + 8} textAnchor="end">{dayMax.toFixed(2)}</text>
        <text className="etf-flow-axis-text" x={pad.left - 8} y={bottomPlot.bottom} textAnchor="end">{dayMin.toFixed(2)}</text>

        <line className="etf-flow-cursor" x1={x(activeIndex)} x2={x(activeIndex)} y1={bottomPlot.top} y2={bottomPlot.bottom} />
        {points.map((point, index) => {
          if (point.daily_value === null) return null
          const barY = yBar(point.daily_value)
          const topY = Math.min(barY, zeroBarY)
          const rectHeight = Math.max(1.5, Math.abs(barY - zeroBarY))
          return <rect
            key={`${point.date}-${index}`}
            x={x(index) - barWidth / 2}
            y={topY}
            width={barWidth}
            height={rectHeight}
            rx="1"
            className={point.daily_value >= 0 ? 'etf-flow-bar-up' : 'etf-flow-bar-down'}
          />
        })}
      </svg>
    </>}

    <p className="etf-flow-footnote">{formatEtfFootnote(data)}</p>
  </section>
}

const holdingColumns: Array<{ key: HoldingSortKey, label: string }> = [
  { key: 'net_inflow_raw', label: '申购净流入' },
  { key: 'streak_days', label: '连续流入天数' },
  { key: 'hold_rate', label: '持仓占比' },
]

export function EtfHoldingsTablePanel({
  symbol,
  market,
  data,
  loading,
  error,
  onRetry,
}: {
  symbol: string
  market?: string
  data?: EtfFundFlowResponse
  loading: boolean
  error?: string
  onRetry: () => void
}) {
  const [sortKey, setSortKey] = useState<HoldingSortKey>('net_inflow_raw')
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc')
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    setSortKey('net_inflow_raw')
    setSortOrder('desc')
    setExpanded(false)
  }, [symbol])

  const items = data?.holdings.items ?? []
  const sortedItems = useMemo(() => {
    const copy = [...items]
    copy.sort((left: EtfHoldingItem, right: EtfHoldingItem) => {
      const a = left[sortKey]
      const b = right[sortKey]
      if (a === null && b === null) return 0
      if (a === null) return 1
      if (b === null) return -1
      return sortOrder === 'asc' ? a - b : b - a
    })
    return copy
  }, [items, sortKey, sortOrder])

  if (loading && !data) {
    return <section className="etf-holdings-panel" aria-label="ETF申购净流入明细">
      <div className="etf-flow-head"><h3>ETF申购净流入明细</h3></div>
      <div className="market-empty-chart">正在加载 ETF 申购明细…</div>
    </section>
  }

  if (error && !data) {
    return <section className="etf-holdings-panel" aria-label="ETF申购净流入明细">
      <div className="etf-flow-head"><h3>ETF申购净流入明细</h3></div>
      <div className="market-capability-gap">
        <strong>ETF申购明细加载失败</strong>
        <span>{error}</span>
        <button type="button" className="inline-retry" onClick={onRetry}>重新加载</button>
      </div>
    </section>
  }

  if (data && data.available && !items.length && data.source_errors?.holdings) {
    return <section className="etf-holdings-panel" aria-label="ETF申购净流入明细">
      <div className="etf-flow-head"><h3>ETF申购净流入明细</h3></div>
      <div className="market-capability-gap">
        <strong>ETF申购明细暂不可用</strong>
        <span>上游持仓明细接口暂时波动，请稍后重试</span>
        <button type="button" className="inline-retry" onClick={onRetry}>重新加载</button>
      </div>
    </section>
  }

  if (!data || !data.available || !items.length) {
    return <section className="etf-holdings-panel" aria-label="ETF申购净流入明细">
      <div className="etf-flow-head"><h3>ETF申购净流入明细</h3></div>
      <p className="etf-flow-empty">{emptyReason(symbol, market)}</p>
    </section>
  }

  const visibleItems = expanded ? sortedItems.slice(0, 30) : sortedItems.slice(0, 5)
  const toggleSort = (nextKey: HoldingSortKey) => {
    if (sortKey === nextKey) {
      setSortOrder((current) => (current === 'desc' ? 'asc' : 'desc'))
    } else {
      setSortKey(nextKey)
      setSortOrder('desc')
    }
  }

  return <section className="etf-holdings-panel" aria-label="ETF申购净流入明细">
    <div className="etf-flow-head">
      <div className="etf-flow-title-group">
        <h3>ETF申购净流入明细</h3>
        <span className="etf-holdings-count">共 {data.holdings.total_count} 只</span>
      </div>
      {items.length > 5 && <button type="button" className="etf-flow-more-link" onClick={() => setExpanded((current) => !current)}>
        {expanded ? '收起' : '更多 >'}
      </button>}
    </div>
    {data.stale && <p className="etf-flow-status-note">当前上游接口波动，已启用本地缓存兜底 <button type="button" className="inline-retry" onClick={onRetry}>刷新</button></p>}

    <div className="etf-holdings-table-wrap">
      <table className="etf-holdings-table">
        <caption className="sr-only">ETF申购净流入明细列表</caption>
        <thead>
          <tr>
            <th scope="col">名称</th>
            {holdingColumns.map((col) => {
              const active = sortKey === col.key
              const arrow = active ? (sortOrder === 'desc' ? '▼' : '▲') : '↕'
              return <th scope="col" key={col.key}>
                <button
                  type="button"
                  className={`etf-sort-button${active ? ' active' : ''}`}
                  aria-label={`按${col.label}排序`}
                  onClick={() => toggleSort(col.key)}
                >
                  <span>{col.label}</span>
                  <i aria-hidden="true">{arrow}</i>
                </button>
              </th>
            })}
          </tr>
        </thead>
        <tbody>
          {visibleItems.map((item) => <tr key={`${item.market}:${item.symbol}`}>
            <th scope="row">
              <div className="etf-holding-name">
                <strong title={item.name}>{item.name}</strong>
                <small>{item.symbol}</small>
              </div>
            </th>
            <td className={`etf-tone-${item.tone}`}>{item.net_inflow_text}</td>
            <td>{item.streak_days ?? '--'}</td>
            <td>{item.hold_rate_text}</td>
          </tr>)}
        </tbody>
      </table>
    </div>
    <p className="etf-flow-footnote">{formatEtfFootnote(data)}</p>
  </section>
}
