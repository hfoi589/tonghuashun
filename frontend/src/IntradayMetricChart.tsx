import { useEffect, useRef, useState } from 'react'
import type { IntradayMetricSeries } from './api'
import './intraday-chart.css'

const CHART_WIDTH = 720
const CHART_HEIGHT = 218
const CHART_LEFT = 58
const CHART_RIGHT = 18
const CHART_TOP = 18
const CHART_BOTTOM = 34

function numericPointValues(series: IntradayMetricSeries): Array<number | null> {
  return series.points.map((point) => {
    if (point.value === null) return null
    const value = Number(point.value)
    return Number.isFinite(value) ? value : null
  })
}

export function IntradayMetricChart({ title, series, directional, precision = 2 }: {
  title: string,
  series: IntradayMetricSeries,
  directional: boolean,
  precision?: number,
}) {
  const lastIndex = Math.max(0, series.points.length - 1)
  const [selectedIndex, setSelectedIndex] = useState(lastIndex)
  const previousPointCount = useRef(series.points.length)

  useEffect(() => {
    setSelectedIndex((current) => {
      const previousLast = Math.max(0, previousPointCount.current - 1)
      return current >= previousLast ? lastIndex : Math.min(current, lastIndex)
    })
    previousPointCount.current = series.points.length
  }, [lastIndex, series.points.length])

  if (series.points.length === 0) {
    return <figure className="intraday-chart intraday-chart-empty">
      <figcaption>
        <div><span>当日分时</span><h4>{title}</h4></div>
      </figcaption>
      <p className="intraday-empty-message">暂无分时数据</p>
    </figure>
  }

  const values = numericPointValues(series)
  const validValues = values.filter((value): value is number => value !== null)
  const domainValues = directional ? [...validValues, 0] : validValues
  let minimum = domainValues.length > 0 ? Math.min(...domainValues) : 0
  let maximum = domainValues.length > 0 ? Math.max(...domainValues) : 1
  if (minimum === maximum) {
    const padding = Math.max(Math.abs(minimum) * .08, 1)
    minimum -= padding
    maximum += padding
  } else {
    const padding = (maximum - minimum) * .08
    minimum -= padding
    maximum += padding
  }
  const plotWidth = CHART_WIDTH - CHART_LEFT - CHART_RIGHT
  const plotHeight = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM
  const xFor = (index: number) => CHART_LEFT + (index / Math.max(1, series.points.length - 1)) * plotWidth
  const yFor = (value: number) => CHART_TOP + ((maximum - value) / (maximum - minimum)) * plotHeight
  let path = ''
  let segmentOpen = false
  values.forEach((value, index) => {
    if (value === null) {
      segmentOpen = false
      return
    }
    path += `${segmentOpen ? ' L' : 'M'} ${xFor(index).toFixed(2)} ${yFor(value).toFixed(2)}`
    segmentOpen = true
  })
  const safeIndex = Math.min(selectedIndex, series.points.length - 1)
  const selectedPoint = series.points[safeIndex]
  const selectedValue = values[safeIndex]
  const latestValue = [...validValues].at(-1) ?? 0
  const tone = latestValue > 0 ? 'up' : latestValue < 0 ? 'down' : 'neutral'
  const midIndex = Math.floor((series.points.length - 1) / 2)
  const tickIndices = [...new Set([0, midIndex, series.points.length - 1])]

  return <figure className={`intraday-chart intraday-tone-${tone}`}>
    <figcaption>
      <div>
        <span>当日分时</span>
        <h4>{title}</h4>
      </div>
      <div className="chart-readout" aria-live="polite">
        <time>{selectedPoint.time}</time>
        <strong>{selectedPoint.value ?? '—'}{selectedPoint.value !== null ? series.unit : ''}</strong>
      </div>
    </figcaption>
    <div className="intraday-chart-frame">
      <svg
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label={`${title}当日分时图`}
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'ArrowLeft') {
            event.preventDefault()
            setSelectedIndex((current) => Math.max(0, current - 1))
          } else if (event.key === 'ArrowRight') {
            event.preventDefault()
            setSelectedIndex((current) => Math.min(series.points.length - 1, current + 1))
          } else if (event.key === 'Home') {
            event.preventDefault()
            setSelectedIndex(0)
          } else if (event.key === 'End') {
            event.preventDefault()
            setSelectedIndex(series.points.length - 1)
          }
        }}
        onPointerMove={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect()
          if (bounds.width === 0) return
          const scaledX = ((event.clientX - bounds.left) / bounds.width) * CHART_WIDTH
          const ratio = Math.max(0, Math.min(1, (scaledX - CHART_LEFT) / plotWidth))
          setSelectedIndex(Math.round(ratio * (series.points.length - 1)))
        }}
      >
        <title>{title}当日分时图，可使用左右方向键查看各时点数值</title>
        {[0, .5, 1].map((ratio) => <line
          className="chart-grid-line"
          key={ratio}
          x1={CHART_LEFT}
          x2={CHART_WIDTH - CHART_RIGHT}
          y1={CHART_TOP + ratio * plotHeight}
          y2={CHART_TOP + ratio * plotHeight}
        />)}
        {minimum <= 0 && maximum >= 0 && <line
          className="chart-zero-line"
          x1={CHART_LEFT}
          x2={CHART_WIDTH - CHART_RIGHT}
          y1={yFor(0)}
          y2={yFor(0)}
        />}
        <path className="chart-series-line" d={path} />
        {selectedValue !== null && <>
          <line className="chart-cursor-line" x1={xFor(safeIndex)} x2={xFor(safeIndex)} y1={CHART_TOP} y2={CHART_TOP + plotHeight} />
          <circle className="chart-active-point" cx={xFor(safeIndex)} cy={yFor(selectedValue)} r="5" />
        </>}
        {tickIndices.map((index) => <text
          className="chart-axis-label"
          key={index}
          x={xFor(index)}
          y={CHART_HEIGHT - 10}
          textAnchor={index === 0 ? 'start' : index === series.points.length - 1 ? 'end' : 'middle'}
        >{series.points[index].time}</text>)}
        <text className="chart-axis-label" x={CHART_LEFT - 8} y={CHART_TOP + 4} textAnchor="end">{maximum.toFixed(precision)}</text>
        <text className="chart-axis-label" x={CHART_LEFT - 8} y={CHART_TOP + plotHeight} textAnchor="end">{minimum.toFixed(precision)}</text>
      </svg>
    </div>
  </figure>
}
