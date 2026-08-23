import { useEffect, useRef, useState } from 'react'
import type { IntradayMetricSeries } from './api'
import { intradayAxisTicks, intradayPointIndexForTime, intradayPointRatio, intradayTimeRatio, nearestIntradayPointIndex } from './intraday-axis'
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

export function IntradayMetricChart({ title, series, directional, precision = 2, selectedTime }: {
  title: string,
  series: IntradayMetricSeries,
  directional: boolean,
  precision?: number,
  selectedTime?: string,
}) {
  const controlled = selectedTime !== undefined
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
  const xFor = (index: number) => CHART_LEFT + intradayPointRatio(
    series.points[index].time,
    index,
    series.points.length,
  ) * plotWidth
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
  const exactSelectedIndex = controlled
    ? series.points.findIndex((point) => point.time === selectedTime)
    : -1
  const safeIndex = controlled
    ? Math.max(0, exactSelectedIndex)
    : Math.min(selectedIndex, series.points.length - 1)
  const selectedPoint = series.points[safeIndex]
  const selectedPointTime = controlled ? selectedTime! : selectedPoint.time
  const selectedValue = controlled && exactSelectedIndex < 0 ? null : values[safeIndex]
  const selectedRawValue = controlled && exactSelectedIndex < 0 ? null : selectedPoint.value
  const controlledRatio = controlled ? intradayTimeRatio(selectedPointTime) : null
  const selectedX = controlledRatio === null
    ? xFor(safeIndex)
    : CHART_LEFT + controlledRatio * plotWidth
  const latestValue = [...validValues].at(-1) ?? 0
  const tone = latestValue > 0 ? 'up' : latestValue < 0 ? 'down' : 'neutral'

  return <figure className={`intraday-chart intraday-tone-${tone}`}>
    <figcaption>
      <div>
        <span>当日分时</span>
        <h4>{title}</h4>
      </div>
      <div className="chart-readout" aria-live={controlled ? undefined : 'polite'}>
        <time>{selectedPointTime}</time>
        <strong>{selectedRawValue ?? '—'}{selectedRawValue !== null ? series.unit : ''}</strong>
      </div>
    </figcaption>
    <div className="intraday-chart-frame">
      <svg
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label={`${title}当日分时图`}
        tabIndex={controlled ? undefined : 0}
        onKeyDown={controlled ? undefined : (event) => {
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
        onPointerMove={controlled ? undefined : (event) => {
          const bounds = event.currentTarget.getBoundingClientRect()
          if (bounds.width === 0) return
          const scaledX = ((event.clientX - bounds.left) / bounds.width) * CHART_WIDTH
          const ratio = Math.max(0, Math.min(1, (scaledX - CHART_LEFT) / plotWidth))
          setSelectedIndex(nearestIntradayPointIndex(series.points, ratio))
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
        <line className="chart-cursor-line" x1={selectedX} x2={selectedX} y1={CHART_TOP} y2={CHART_TOP + plotHeight} />
        {selectedValue !== null && <>
          <circle className="chart-active-point" cx={selectedX} cy={yFor(selectedValue)} r="5" />
        </>}
        {intradayAxisTicks.map((tick) => <text
          className="chart-axis-label intraday-x-axis-label"
          key={tick.label}
          x={CHART_LEFT + tick.ratio * plotWidth}
          y={CHART_HEIGHT - 10}
          textAnchor={tick.anchor}
        >{tick.label}</text>)}
        <text className="chart-axis-label" x={CHART_LEFT - 8} y={CHART_TOP + 4} textAnchor="end">{maximum.toFixed(precision)}</text>
        <text className="chart-axis-label" x={CHART_LEFT - 8} y={CHART_TOP + plotHeight} textAnchor="end">{minimum.toFixed(precision)}</text>
      </svg>
    </div>
  </figure>
}

const MACD_DIF_COLOR = '#30343a'
const MACD_DEA_COLOR = '#f06b35'

function seriesValueMap(series: IntradayMetricSeries): Map<string, number | null> {
  return new Map(series.points.map((point) => {
    const value = point.value === null ? null : Number(point.value)
    return [point.time, value !== null && Number.isFinite(value) ? value : null]
  }))
}

export function IntradayMacdChart({ dif, dea, macd, selectedTime: controlledTime }: {
  dif: IntradayMetricSeries,
  dea: IntradayMetricSeries,
  macd: IntradayMetricSeries,
  selectedTime?: string,
}) {
  const controlled = controlledTime !== undefined
  const reference = [macd, dif, dea].find((series) => series.points.length > 0) ?? macd
  const lastIndex = Math.max(0, reference.points.length - 1)
  const [selectedIndex, setSelectedIndex] = useState(lastIndex)
  const previousPointCount = useRef(reference.points.length)

  useEffect(() => {
    setSelectedIndex((current) => {
      const previousLast = Math.max(0, previousPointCount.current - 1)
      return current >= previousLast ? lastIndex : Math.min(current, lastIndex)
    })
    previousPointCount.current = reference.points.length
  }, [lastIndex, reference.points.length])

  if (reference.points.length === 0) {
    return <figure className="intraday-chart intraday-chart-empty">
      <figcaption><div><span>当日分时</span><h4>MACD</h4></div></figcaption>
      <p className="intraday-empty-message">暂无分时数据</p>
    </figure>
  }

  const difValues = seriesValueMap(dif)
  const deaValues = seriesValueMap(dea)
  const macdValues = seriesValueMap(macd)
  const validValues = [...difValues.values(), ...deaValues.values(), ...macdValues.values()]
    .filter((value): value is number => value !== null)
  let minimum = Math.min(0, ...validValues)
  let maximum = Math.max(0, ...validValues)
  if (minimum === maximum) {
    minimum -= 1
    maximum += 1
  } else {
    const padding = (maximum - minimum) * .08
    minimum -= padding
    maximum += padding
  }
  const plotWidth = CHART_WIDTH - CHART_LEFT - CHART_RIGHT
  const plotHeight = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM
  const xFor = (time: string, index: number, count: number) => CHART_LEFT + intradayPointRatio(time, index, count) * plotWidth
  const yFor = (value: number) => CHART_TOP + ((maximum - value) / (maximum - minimum)) * plotHeight
  const linePath = (series: IntradayMetricSeries) => {
    let path = ''
    let segmentOpen = false
    series.points.forEach((point, index) => {
      const value = point.value === null ? null : Number(point.value)
      if (value === null || !Number.isFinite(value)) {
        segmentOpen = false
        return
      }
      path += `${segmentOpen ? ' L' : 'M'} ${xFor(point.time, index, series.points.length).toFixed(2)} ${yFor(value).toFixed(2)}`
      segmentOpen = true
    })
    return path
  }
  const safeIndex = controlled
    ? intradayPointIndexForTime(reference.points, controlledTime)
    : Math.min(selectedIndex, reference.points.length - 1)
  const selectedPointTime = controlled ? controlledTime! : reference.points[safeIndex].time
  const selectedDif = difValues.get(selectedPointTime) ?? null
  const selectedDea = deaValues.get(selectedPointTime) ?? null
  const selectedMacd = macdValues.get(selectedPointTime) ?? null
  const zeroY = yFor(0)
  const barWidth = Math.max(1.5, plotWidth / 240 * .72)
  const valueLabel = (value: number | null) => value === null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(3)}`

  return <figure className="intraday-chart intraday-macd-chart">
    <figcaption>
      <div><span>当日分时</span><h4>MACD</h4></div>
      <div className="chart-readout macd-readout" aria-live={controlled ? undefined : 'polite'}>
        <time>{selectedPointTime}</time>
        <b style={{ color: MACD_DIF_COLOR }}>DIF</b><strong style={{ color: MACD_DIF_COLOR }}>{valueLabel(selectedDif)}</strong>
        <b style={{ color: MACD_DEA_COLOR }}>DEA</b><strong style={{ color: MACD_DEA_COLOR }}>{valueLabel(selectedDea)}</strong>
        <b>MACD</b><strong className={selectedMacd === null ? undefined : selectedMacd < 0 ? 'macd-down' : 'macd-up'}>{valueLabel(selectedMacd)}</strong>
      </div>
    </figcaption>
    <div className="intraday-chart-frame">
      <svg
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label="MACD当日分时图"
        tabIndex={controlled ? undefined : 0}
        onKeyDown={controlled ? undefined : (event) => {
          if (event.key === 'ArrowLeft') {
            event.preventDefault()
            setSelectedIndex((current) => Math.max(0, current - 1))
          } else if (event.key === 'ArrowRight') {
            event.preventDefault()
            setSelectedIndex((current) => Math.min(reference.points.length - 1, current + 1))
          } else if (event.key === 'Home') {
            event.preventDefault()
            setSelectedIndex(0)
          } else if (event.key === 'End') {
            event.preventDefault()
            setSelectedIndex(reference.points.length - 1)
          }
        }}
        onPointerMove={controlled ? undefined : (event) => {
          const bounds = event.currentTarget.getBoundingClientRect()
          if (bounds.width === 0) return
          const scaledX = ((event.clientX - bounds.left) / bounds.width) * CHART_WIDTH
          const ratio = Math.max(0, Math.min(1, (scaledX - CHART_LEFT) / plotWidth))
          setSelectedIndex(nearestIntradayPointIndex(reference.points, ratio))
        }}
      >
        <title>MACD当日分时图，包含 DIF、DEA 和 MACD，可使用左右方向键查看各时点数值</title>
        {[0, .5, 1].map((ratio) => <line className="chart-grid-line" key={ratio} x1={CHART_LEFT} x2={CHART_WIDTH - CHART_RIGHT} y1={CHART_TOP + ratio * plotHeight} y2={CHART_TOP + ratio * plotHeight} />)}
        <line className="chart-zero-line" x1={CHART_LEFT} x2={CHART_WIDTH - CHART_RIGHT} y1={zeroY} y2={zeroY} />
        {macd.points.map((point, index) => {
          const value = point.value === null ? null : Number(point.value)
          if (value === null || !Number.isFinite(value)) return null
          const x = xFor(point.time, index, macd.points.length)
          const y = yFor(value)
          return <rect
            className={value < 0 ? 'macd-histogram-down' : 'macd-histogram-up'}
            key={`${point.time}-${index}`}
            x={x - barWidth / 2}
            y={Math.min(y, zeroY)}
            width={barWidth}
            height={Math.max(1, Math.abs(zeroY - y))}
          />
        })}
        <path className="macd-dif-line" d={linePath(dif)} />
        <path className="macd-dea-line" d={linePath(dea)} />
        <line
          className="chart-cursor-line"
          x1={xFor(selectedPointTime, safeIndex, reference.points.length)}
          x2={xFor(selectedPointTime, safeIndex, reference.points.length)}
          y1={CHART_TOP}
          y2={CHART_TOP + plotHeight}
        />
        {intradayAxisTicks.map((tick) => <text className="chart-axis-label intraday-x-axis-label" key={tick.label} x={CHART_LEFT + tick.ratio * plotWidth} y={CHART_HEIGHT - 10} textAnchor={tick.anchor}>{tick.label}</text>)}
        <text className="chart-axis-label" x={CHART_LEFT - 8} y={CHART_TOP + 4} textAnchor="end">{maximum.toFixed(3)}</text>
        <text className="chart-axis-label" x={CHART_LEFT - 8} y={CHART_TOP + plotHeight} textAnchor="end">{minimum.toFixed(3)}</text>
      </svg>
    </div>
  </figure>
}
