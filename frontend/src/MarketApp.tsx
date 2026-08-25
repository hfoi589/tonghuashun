import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from './api'
import { DailyKChart, type DailyKSelection } from './DailyKChart'
import { IntradayMacdChart, IntradayMetricChart } from './IntradayMetricChart'
import { intradayAxisTicks, intradayPointIndexForTime, intradayPointRatio, intradayTimeRatio, nearestIntradayPointIndex } from './intraday-axis'
import {
  marketApi,
  marketStreamUrl,
  type KlineBar,
  type MarketSeriesPage,
  type MarketSnapshot,
  type MarketUser,
  type TimesharePoint,
  type WatchlistGroup,
  type WatchlistItem,
} from './market-api'
import './market.css'

type AuthState = 'loading' | 'anonymous' | 'authenticated'
type ChartPeriod = 'timeshare' | 'five_day' | 'day' | 'week' | 'month'

const chartPeriods: Array<[ChartPeriod, string]> = [
  ['timeshare', '分时'],
  ['day', '日K'],
  ['five_day', '五日'],
  ['week', '周K'],
  ['month', '月K'],
]

function tone(value: string | null | undefined): 'up' | 'down' | 'flat' {
  const number = Number(String(value ?? '').replace('%', ''))
  return number > 0 ? 'up' : number < 0 ? 'down' : 'flat'
}

function display(value: string | null | undefined, suffix = ''): string {
  return value === null || value === undefined || value === '' ? '—' : `${value}${suffix}`
}

function fixed(value: string | null | undefined, places = 2): string {
  const number = Number(value)
  return value === null || value === undefined || value === '' || !Number.isFinite(number)
    ? '—'
    : number.toFixed(places)
}

function compact(value: string | null | undefined, unit: '股' | '元'): string {
  const number = Number(value)
  if (value === null || value === undefined || value === '' || !Number.isFinite(number)) return '—'
  if (Math.abs(number) >= 100_000_000) return `${(number / 100_000_000).toFixed(2)}亿${unit}`
  if (Math.abs(number) >= 10_000) return `${(number / 10_000).toFixed(2)}万${unit}`
  return `${number.toFixed(2)}${unit}`
}

function percentChange(close: string | null, previousClose: string | null): string | null {
  const closeValue = Number(close)
  const previousValue = Number(previousClose)
  if (!Number.isFinite(closeValue) || !Number.isFinite(previousValue) || previousValue === 0) return null
  const value = (closeValue - previousValue) / previousValue * 100
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}

export function nextIntradaySelectedTime(
  current: string | undefined,
  previousLatest: string | undefined,
  times: string[],
): string | undefined {
  const latest = times.at(-1)
  if (latest === undefined) return current
  const previousLatestRatio = previousLatest === undefined ? null : intradayTimeRatio(previousLatest)
  const latestRatio = intradayTimeRatio(latest)
  const latestMovedBackward = previousLatestRatio !== null && latestRatio !== null && latestRatio < previousLatestRatio
  return current === undefined || current === previousLatest || !times.includes(current) || latestMovedBackward
    ? latest
    : current
}

function LineChart({ name, points, selectedTime, onSelectionChange }: {
  name: string,
  points: TimesharePoint[],
  selectedTime?: string,
  onSelectionChange: (time: string) => void,
}) {
  const priceValues = points.map((point) => {
    if (point.price === null || point.price === '') return null
    const value = Number(point.price)
    return Number.isFinite(value) ? value : null
  })
  const data = priceValues.flatMap((value, index) => value === null ? [] : [{ index, value }])
  if (data.length === 0) return <div className="market-empty-chart">暂无 App 内部分时数据</div>
  const width = 900
  const height = 360
  const pad = { top: 22, right: 18, bottom: 38, left: 58 }
  let min = Math.min(...data.map((item) => item.value))
  let max = Math.max(...data.map((item) => item.value))
  if (min === max) { min -= 0.01; max += 0.01 }
  const spread = max - min
  min -= spread * 0.08
  max += spread * 0.08
  const plotWidth = width - pad.left - pad.right
  const plotHeight = height - pad.top - pad.bottom
  const x = (index: number) => pad.left + intradayPointRatio(points[index].time, index, points.length) * plotWidth
  const y = (value: number) => pad.top + (max - value) / (max - min) * plotHeight
  let path = ''
  let segmentOpen = false
  priceValues.forEach((value, index) => {
    if (value === null) {
      segmentOpen = false
      return
    }
    path += `${segmentOpen ? ' L' : 'M'}${x(index).toFixed(2)},${y(value).toFixed(2)}`
    segmentOpen = true
  })
  const area = priceValues.every((value) => value !== null)
    ? `${path} L${x(data.at(-1)!.index)},${height - pad.bottom} L${x(data[0].index)},${height - pad.bottom} Z`
    : null
  const selectedIndex = intradayPointIndexForTime(points, selectedTime)
  const selectedPoint = points[selectedIndex]
  const selectedPrice = selectedPoint.price === null || selectedPoint.price === ''
    ? null
    : Number(selectedPoint.price)
  const selectedX = x(selectedIndex)
  const selectedY = selectedPrice !== null && Number.isFinite(selectedPrice) ? y(selectedPrice) : null
  const selectAtClientX = (element: SVGSVGElement, clientX: number) => {
    const bounds = element.getBoundingClientRect()
    if (bounds.width === 0) return
    const scaledX = ((clientX - bounds.left) / bounds.width) * width
    const ratio = Math.max(0, Math.min(1, (scaledX - pad.left) / plotWidth))
    onSelectionChange(points[nearestIntradayPointIndex(points, ratio)].time)
  }
  return <figure className="market-timeshare-chart">
    <figcaption>
      <div><span>当日分时</span><h3>{name}</h3></div>
      <div className="market-timeshare-readout" aria-live="polite">
        <time>{selectedPoint.time}</time>
        <span>价格</span><strong>{fixed(selectedPoint.price)}</strong>
        {selectedPoint.average_price !== null && <><span>均价</span><b>{fixed(selectedPoint.average_price)}</b></>}
        {selectedPoint.volume !== null && <><span>量</span><b>{compact(selectedPoint.volume, '股')}</b></>}
      </div>
    </figcaption>
    <div className="market-timeshare-chart-frame">
    <svg
      className="market-price-chart market-timeshare-price-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${name}分时价格图`}
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'ArrowLeft') {
          event.preventDefault()
          onSelectionChange(points[Math.max(0, selectedIndex - 1)].time)
        } else if (event.key === 'ArrowRight') {
          event.preventDefault()
          onSelectionChange(points[Math.min(points.length - 1, selectedIndex + 1)].time)
        } else if (event.key === 'Home') {
          event.preventDefault()
          onSelectionChange(points[0].time)
        } else if (event.key === 'End') {
          event.preventDefault()
          onSelectionChange(points.at(-1)!.time)
        }
      }}
      onPointerMove={(event) => {
        selectAtClientX(event.currentTarget, event.clientX)
      }}
      onPointerDown={(event) => {
        event.currentTarget.setPointerCapture?.(event.pointerId)
        selectAtClientX(event.currentTarget, event.clientX)
      }}
      onPointerUp={(event) => {
        if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId)
        }
      }}
      onPointerLeave={(event) => {
        if (event.pointerType === 'mouse') onSelectionChange(points.at(-1)!.time)
      }}
    >
      <title>{name}分时价格图，可移动十字线并同步查看下方全部指标</title>
      {[0, 1, 2, 3, 4].map((tick) => {
        const py = pad.top + tick / 4 * plotHeight
        const value = max - tick / 4 * (max - min)
        return <g key={tick}><line x1={pad.left} y1={py} x2={width - pad.right} y2={py} /><text x={pad.left - 9} y={py + 4} textAnchor="end">{value.toFixed(2)}</text></g>
      })}
      {area !== null && <path className="market-chart-area" d={area} />}
      <path className="market-chart-line" d={path} />
      <line className="market-timeshare-cursor-x" x1={selectedX} x2={selectedX} y1={pad.top} y2={pad.top + plotHeight} />
      {selectedY !== null && <>
        <line className="market-timeshare-cursor-y" x1={pad.left} x2={width - pad.right} y1={selectedY} y2={selectedY} />
        <circle className="market-timeshare-active-point" cx={selectedX} cy={selectedY} r="5" />
      </>}
      {intradayAxisTicks.map((tick) => <text className="market-intraday-x-axis-label" key={tick.label} x={pad.left + tick.ratio * plotWidth} y={height - 12} textAnchor={tick.anchor}>{tick.label}</text>)}
    </svg>
    </div>
  </figure>
}

function CandleChart({ name, bars }: { name: string, bars: KlineBar[] }) {
  const data = bars.filter((bar) => [bar.open, bar.high, bar.low, bar.close].every((value) => Number.isFinite(Number(value))))
  if (data.length === 0) return <div className="market-empty-chart">暂无 App 内部 K 线数据</div>
  const width = 900
  const height = 360
  const values = data.flatMap((bar) => [Number(bar.high), Number(bar.low)])
  const min = Math.min(...values)
  const max = Math.max(...values)
  const y = (value: number) => 24 + (max - value) / Math.max(.0001, max - min) * 292
  const step = 840 / data.length
  return <svg className="market-price-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${name}K线图`}>
    {data.map((bar, index) => {
      const open = Number(bar.open)
      const close = Number(bar.close)
      const cx = 42 + index * step + step / 2
      const top = y(Math.max(open, close))
      const bottom = y(Math.min(open, close))
      const rising = close >= open
      return <g key={`${bar.time}-${index}`} className={rising ? 'candle-up' : 'candle-down'}>
        <line x1={cx} y1={y(Number(bar.high))} x2={cx} y2={y(Number(bar.low))} />
        <rect x={cx - Math.max(1, step * .28)} y={top} width={Math.max(2, step * .56)} height={Math.max(1, bottom - top)} />
      </g>
    })}
  </svg>
}

function DailyKPanel({ name, page, loading, error, onSelectionChange, onRetry }: {
  name: string,
  page?: MarketSeriesPage,
  loading: boolean,
  error?: string,
  onSelectionChange: (selection: DailyKSelection) => void,
  onRetry: () => void,
}) {
  if (loading) return <div className="market-empty-chart">正在加载前复权日 K…</div>
  if (error && !page) return <div className="market-capability-gap"><strong>日 K 加载失败</strong><span>{error}</span><button className="daily-k-retry" type="button" onClick={onRetry}>重新加载日 K</button></div>
  if (!page || page.bars.length === 0) return <div className="market-capability-gap">
    <strong>日 K 数据暂不可用</strong>
    <span>{page?.source_error ?? error ?? 'KLINE_SOURCES_UNAVAILABLE'}</span>
    {page && Object.entries(page.source_errors).map(([source, code]) => code && <small key={source}>{source}: {code}</small>)}
    <button className="daily-k-retry" type="button" onClick={onRetry}>重新加载日 K</button>
  </div>
  const sourceLabel = page.source === 'THS_PUBLIC'
    ? '10jqka 公开源'
    : page.source === 'THS_APP'
      ? 'core_metrics App'
      : '未知来源'
  return <div className="daily-k-panel">
    <div className="daily-k-status">
      <span>前复权</span>
      <span>{sourceLabel}</span>
      {page.cached && <span>缓存</span>}
      {page.stale && <span className="warning">过期缓存</span>}
      {page.source_error && <span className="warning">{page.source_error}</span>}
      {page.stale && Object.entries(page.source_errors).map(([source, code]) => code && <span className="warning" key={source}>{source}: {code}</span>)}
    </div>
    <DailyKChart name={name} onSelectionChange={onSelectionChange} page={page} />
  </div>
}

function PasswordChange({ user, onDone }: { user: MarketUser, onDone: (user: MarketUser) => void }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [message, setMessage] = useState('')
  async function submit(event: FormEvent) {
    event.preventDefault()
    setMessage('')
    try {
      await marketApi.changePassword(current, next, confirmation)
      onDone({ ...user, must_change_password: false })
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '密码修改失败')
    }
  }
  return <main className="market-auth-shell">
    <section className="market-auth-card">
      <div className="market-brand-mark" aria-hidden="true">顺</div>
      <p className="market-kicker">首次登录</p>
      <h1>设置你的新密码</h1>
      <p>管理员提供的是临时密码。完成修改后才能读取自选和行情。</p>
      <form onSubmit={submit}>
        <label htmlFor="market-current-password">当前临时密码</label>
        <input id="market-current-password" type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} required />
        <label htmlFor="market-new-password">新密码</label>
        <input id="market-new-password" type="password" autoComplete="new-password" minLength={8} value={next} onChange={(event) => setNext(event.target.value)} required />
        <label htmlFor="market-confirm-password">确认新密码</label>
        <input id="market-confirm-password" type="password" autoComplete="new-password" minLength={8} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} required />
        <button type="submit">保存并进入行情</button>
      </form>
      {message && <p className="market-form-error" role="alert">{message}</p>}
    </section>
  </main>
}

function Login({ onLogin }: { onLogin: (user: MarketUser) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setMessage('')
    try { onLogin(await marketApi.login(username, password)) }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : '登录失败') }
    finally { setBusy(false) }
  }
  return <main className="market-auth-shell">
    <section className="market-auth-card">
      <div className="market-brand-mark" aria-hidden="true">顺</div>
      <p className="market-kicker">THS MARKET</p>
      <h1>登录行情中心</h1>
      <p>访问你的分组自选和 App 内部实时行情。</p>
      <form onSubmit={submit}>
        <label htmlFor="market-username">用户名</label>
        <input id="market-username" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required />
        <label htmlFor="market-password">密码</label>
        <input id="market-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required />
        <button type="submit" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
      </form>
      {message && <p className="market-form-error" role="alert">{message}</p>}
    </section>
  </main>
}

function FundFlow({ values }: { values: MarketSnapshot['main_fund_flow'] }) {
  const periods = [['today', '当日'], ['three_day', '3日'], ['five_day', '5日']] as const
  const rows = [['main_net_inflow', '主力净流入'], ['main_visible_inflow', '主力明盘'], ['main_hidden_inflow', '主力暗盘'], ['retail_inflow', '散户流入']] as const
  if (Object.keys(values).length === 0) return null
  return <section className="market-section market-fund-flow">
    <div className="market-section-title"><div><span>CAPITAL FLOW</span><h3>主力流向</h3></div><small>App 内部接口</small></div>
    <table className="market-fund-table"><thead><tr><th>指标</th>{periods.map(([, label]) => <th key={label}>{label}</th>)}</tr></thead>
      <tbody>{rows.map(([field, label]) => <tr key={field}><th>{label}</th>{periods.map(([period]) => {
        const value = values[period]?.[field]
        const unit = values[period]?.unit ?? ''
        return <td key={period} className={`market-number-${tone(value)}`}>{display(value, unit)}</td>
      })}</tr>)}</tbody>
    </table>
  </section>
}

const marketIntradayCharts = [
  ['large_order_net', '大单净量', true, 2],
  ['large_order_amount', '大单金额', true, 1],
  ['retail_count', '散户数量', false, 2],
] as const

function MarketIntradayCharts({ symbol, series, selectedTime }: {
  symbol: string,
  series: MarketSnapshot['intraday_series'],
  selectedTime?: string,
}) {
  return <div className="market-intraday-list">
    {marketIntradayCharts.map(([key, title, directional, precision]) => <IntradayMetricChart
      compact
      directional={directional}
      key={`${symbol}-${key}`}
      precision={precision}
      selectedTime={selectedTime}
      showXAxis={false}
      series={series[key] ?? { unit: key === 'large_order_amount' ? '万' : null, points: [] }}
      title={title}
    />)}
    <IntradayMacdChart
      compact
      key={`${symbol}-macd`}
      dea={series.macd_dea ?? { unit: null, points: [] }}
      dif={series.macd_dif ?? { unit: null, points: [] }}
      macd={series.macdfs ?? { unit: null, points: [] }}
      selectedTime={selectedTime}
      showXAxis={false}
    />
  </div>
}

function Detail({ item, snapshot, period, setPeriod, series, dailyLoading, dailyError, onRetryDaily }: {
  item: { symbol: string, name: string },
  snapshot?: MarketSnapshot,
  period: ChartPeriod,
  setPeriod: (period: ChartPeriod) => void,
  series?: MarketSeriesPage,
  dailyLoading: boolean,
  dailyError?: string,
  onRetryDaily: () => void,
}) {
  const quote = snapshot?.quote ?? {}
  const latestIntradayTime = snapshot?.timeshare.at(-1)?.time
  const intradayTimes = snapshot?.timeshare.map((point) => point.time) ?? []
  const intradayTimesKey = intradayTimes.join('|')
  const [intradaySelectedTime, setIntradaySelectedTime] = useState<string>()
  const previousLatestIntradayTime = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (latestIntradayTime === undefined) return
    const previousLatest = previousLatestIntradayTime.current
    setIntradaySelectedTime((current) => nextIntradaySelectedTime(current, previousLatest, intradayTimes))
    previousLatestIntradayTime.current = latestIntradayTime
  }, [intradayTimesKey, latestIntradayTime])
  const [dailySelection, setDailySelection] = useState<DailyKSelection | null>(null)
  useEffect(() => {
    setDailySelection(null)
  }, [item.symbol])
  useEffect(() => {
    if (period !== 'day') setDailySelection(null)
  }, [period])
  const selectedDailyBar = period === 'day' ? dailySelection?.bar : undefined
  const previousDailyClose = selectedDailyBar && dailySelection && dailySelection.index > 0
    ? series?.bars[dailySelection.index - 1]?.close ?? null
    : null
  const selectedDailyChange = selectedDailyBar
    ? percentChange(selectedDailyBar.close, previousDailyClose)
    : null
  const displayedPrice = selectedDailyBar ? fixed(selectedDailyBar.close) : display(quote.price)
  const displayedChange = selectedDailyBar ? display(selectedDailyChange) : display(quote.change_percent)
  const changeTone = tone(selectedDailyBar ? selectedDailyChange : quote.change_percent)
  const klineAvailable = snapshot?.capabilities.kline?.available === true
  const quoteFields = selectedDailyBar
    ? [
      ['开盘', fixed(selectedDailyBar.open)],
      ['最高', fixed(selectedDailyBar.high)],
      ['最低', fixed(selectedDailyBar.low)],
      ['收盘', fixed(selectedDailyBar.close)],
      ['成交量', compact(selectedDailyBar.volume, '股')],
      ['成交额', compact(selectedDailyBar.amount, '元')],
    ]
    : [
      ['今开', display(quote.open)],
      ['最高', display(quote.high)],
      ['最低', display(quote.low)],
      ['昨收', display(quote.previous_close)],
      ['换手', display(quote.turnover_rate)],
      ['成交量', display(quote.volume)],
    ]
  return <section className="market-detail">
    <header className="market-quote-head">
      <div><p>{item.symbol} · {selectedDailyBar ? `${selectedDailyBar.time} 日K` : '实时行情'}</p><h1>{snapshot?.name ?? item.name}</h1></div>
      <div className={`market-last-price market-number-${changeTone}`}><strong>{displayedPrice}</strong><span>{displayedChange}</span></div>
      <div className="market-live-status"><i className={snapshot?.stale ? 'stale' : ''} />{selectedDailyBar ? '十字线选中日' : snapshot?.source_time ? `${snapshot.source_time} 更新` : '正在读取 App 接口'}</div>
    </header>
    <div className="market-quote-strip">
      {quoteFields.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
    </div>
    <section className="market-chart-panel">
      <nav className="market-period-tabs" aria-label="图表周期">{chartPeriods.map(([value, label]) => <button type="button" key={value} className={period === value ? 'active' : ''} onClick={() => setPeriod(value)}>{label}</button>)}</nav>
      <section className="market-metric-grid">
        {[
          ['大单净量', quote.large_order_net], ['大单金额', quote.large_order_amount], ['散户数量', quote.retail_count], ['MACDFS', quote.macdfs],
        ].map(([label, value]) => <article key={label}><span>{label}</span><strong className={`market-number-${tone(value)}`}>{display(value)}</strong></article>)}
      </section>
      {snapshot && <FundFlow values={snapshot.main_fund_flow} />}
      {period === 'timeshare'
        ? <>
          <LineChart name={snapshot?.name ?? item.name} points={snapshot?.timeshare ?? []} selectedTime={intradaySelectedTime} onSelectionChange={setIntradaySelectedTime} />
          <MarketIntradayCharts symbol={snapshot?.symbol ?? item.symbol} series={snapshot?.intraday_series ?? {}} selectedTime={intradaySelectedTime} />
        </>
        : period === 'day'
          ? <DailyKPanel name={snapshot?.name ?? item.name} page={series} loading={dailyLoading} error={dailyError} onSelectionChange={setDailySelection} onRetry={onRetryDaily} />
        : !klineAvailable
          ? <div className="market-capability-gap"><strong>App 内部 K 线接口尚未确认</strong><span>为避免展示错误行情，这里不会使用网页源、OCR 或猜测参数。</span></div>
          : <CandleChart name={snapshot?.name ?? item.name} bars={series?.bars ?? []} />}
    </section>
    <section className="market-section market-level2">
      <div className="market-section-title"><div><span>LEVEL 2</span><h3>盘口与逐笔</h3></div></div>
      <div className="market-capability-grid">
        <div><strong>十档盘口</strong><span>{snapshot?.capabilities.order_book?.available ? '实时可用' : '当前 App 接口未确认'}</span></div>
        <div><strong>逐笔成交</strong><span>{snapshot?.capabilities.trades?.available ? '实时可用' : '当前 App 接口未确认'}</span></div>
      </div>
    </section>
  </section>
}

export function MarketApp() {
  const [auth, setAuth] = useState<AuthState>('loading')
  const [user, setUser] = useState<MarketUser | null>(null)
  const [groups, setGroups] = useState<WatchlistGroup[]>([])
  const [groupId, setGroupId] = useState<number | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [snapshots, setSnapshots] = useState<Record<string, MarketSnapshot>>({})
  const [period, setPeriod] = useState<ChartPeriod>('timeshare')
  const [legacySeries, setLegacySeries] = useState<MarketSeriesPage>()
  const [dailySeries, setDailySeries] = useState<Record<string, MarketSeriesPage>>({})
  const [dailyLoading, setDailyLoading] = useState<Record<string, boolean>>({})
  const [dailyErrors, setDailyErrors] = useState<Record<string, string>>({})
  const [dailyRetryVersion, setDailyRetryVersion] = useState(0)
  const [symbol, setSymbol] = useState('')
  const [message, setMessage] = useState('')
  const [removingSymbol, setRemovingSymbol] = useState<string | null>(null)
  const [connection, setConnection] = useState<'live' | 'reconnecting' | 'unavailable'>('unavailable')
  const socket = useRef<WebSocket | null>(null)
  const requestedDailySeries = useRef(new Set<string>())

  const allItems = useMemo(() => {
    const found = new Map<string, { symbol: string, name: string, market: string }>()
    groups.forEach((group) => group.items.forEach((item) => found.set(item.symbol, item)))
    return [...found.values()]
  }, [groups])
  const activeGroup = groups.find((group) => group.id === groupId) ?? groups[0]
  const selectedItem = allItems.find((item) => item.symbol === selected)
  const selectedKlineAvailable = selected ? snapshots[selected]?.capabilities.kline?.available === true : false

  async function loadWatchlists() {
    const result = await marketApi.watchlists()
    setGroups(result.groups)
    const availableItems = result.groups.flatMap((group) => group.items)
    const availableSymbols = new Set(availableItems.map((item) => item.symbol))
    setGroupId((current) => current !== null && result.groups.some((group) => group.id === current)
      ? current
      : result.groups[0]?.id ?? null)
    setSelected((current) => current !== null && availableSymbols.has(current)
      ? current
      : availableItems[0]?.symbol ?? null)
  }

  useEffect(() => {
    let active = true
    void marketApi.session().then((current) => {
      if (!active) return
      setUser(current)
      setAuth('authenticated')
      if (!current.must_change_password) void loadWatchlists().catch((reason) => setMessage(reason instanceof Error ? reason.message : '自选加载失败'))
    }).catch((reason) => {
      if (!active) return
      setAuth(reason instanceof ApiError && reason.status === 401 ? 'anonymous' : 'anonymous')
      if (!(reason instanceof ApiError && reason.status === 401)) setMessage(reason instanceof Error ? reason.message : '服务暂时不可用')
    })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!selected || auth !== 'authenticated' || user?.must_change_password) return
    let active = true
    void marketApi.snapshot(selected).then((snapshot) => {
      if (active) setSnapshots((current) => ({ ...current, [snapshot.symbol]: snapshot }))
    }).catch((reason) => { if (active) setMessage(reason instanceof Error ? reason.message : '行情读取失败') })
    return () => { active = false }
  }, [auth, selected, user?.must_change_password])

  useEffect(() => {
    if (!selected || auth !== 'authenticated' || user?.must_change_password || requestedDailySeries.current.has(selected)) return
    requestedDailySeries.current.add(selected)
    const requestedSymbol = selected
    setDailyLoading((current) => ({ ...current, [requestedSymbol]: true }))
    setDailyErrors((current) => {
      const next = { ...current }
      delete next[requestedSymbol]
      return next
    })
    void marketApi.series(requestedSymbol, 'day').then((value) => {
      setDailySeries((current) => ({ ...current, [requestedSymbol]: value }))
    }).catch((reason) => {
      requestedDailySeries.current.delete(requestedSymbol)
      setDailyErrors((current) => ({ ...current, [requestedSymbol]: reason instanceof Error ? reason.message : '日 K 读取失败' }))
    }).finally(() => {
      setDailyLoading((current) => ({ ...current, [requestedSymbol]: false }))
    })
  }, [auth, dailyRetryVersion, selected, user?.must_change_password])

  function retryDailySeries(symbol: string) {
    requestedDailySeries.current.delete(symbol)
    setDailySeries((current) => {
      const next = { ...current }
      delete next[symbol]
      return next
    })
    setDailyErrors((current) => {
      const next = { ...current }
      delete next[symbol]
      return next
    })
    setDailyRetryVersion((current) => current + 1)
  }

  useEffect(() => {
    if (period === 'timeshare' || period === 'day' || !selected || !selectedKlineAvailable) {
      setLegacySeries(undefined)
      return
    }
    let active = true
    void marketApi.series(selected, period).then((value) => { if (active) setLegacySeries(value) }).catch((reason) => { if (active) setMessage(reason instanceof Error ? reason.message : 'K 线读取失败') })
    return () => { active = false }
  }, [period, selected, selectedKlineAvailable])

  useEffect(() => {
    socket.current?.close()
    socket.current = null
    if (auth !== 'authenticated' || user?.must_change_password || typeof WebSocket === 'undefined') return
    const url = marketStreamUrl()
    if (!url) return
    let client: WebSocket
    try { client = new WebSocket(url) } catch { return }
    socket.current = client
    client.onopen = () => {
      setConnection('live')
      client.send(JSON.stringify({ type: 'subscribe', watchlist: allItems.map((item) => item.symbol), detail: selected }))
    }
    client.onmessage = (event) => {
      try {
        const payload = JSON.parse(String(event.data)) as { type: string, data?: MarketSnapshot, symbol?: string }
        if (payload.type === 'snapshot' && payload.data) setSnapshots((current) => ({ ...current, [payload.data!.symbol]: payload.data! }))
        if (payload.type === 'source_status') setConnection('reconnecting')
      } catch { /* Ignore malformed external frames. */ }
    }
    client.onclose = () => setConnection('reconnecting')
    client.onerror = () => setConnection('reconnecting')
    return () => client.close()
  }, [allItems, auth, selected, user?.must_change_password])

  async function addSymbol(event: FormEvent) {
    event.preventDefault()
    if (!activeGroup || !/^[0-9]{6}$/.test(symbol)) return
    setMessage('')
    try {
      const lookup = await marketApi.lookupSymbol(symbol)
      await marketApi.addSymbol(activeGroup.id, lookup.symbol)
      setSymbol('')
      await loadWatchlists()
      setSelected(lookup.symbol)
      const primaryGroup = groups.find((group) => group.is_primary) ?? groups[0]
      const synchronized = primaryGroup && activeGroup.id !== primaryGroup.id
      setMessage(`已添加 ${lookup.name}（${lookup.symbol}）到${activeGroup.name}${synchronized ? '，并同步到自选' : ''}`)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '添加自选失败') }
  }

  async function createGroup() {
    const name = window.prompt('输入新分组名称')?.trim()
    if (!name) return
    try {
      const group = await marketApi.createGroup(name)
      await loadWatchlists()
      setGroupId(group.id)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '创建分组失败') }
  }

  async function removeSymbol(item: WatchlistItem, group: WatchlistGroup) {
    const confirmed = window.confirm(`从自选和所有分组删除 ${item.name}（${item.symbol}）？`)
    if (!confirmed) return
    setMessage('')
    setRemovingSymbol(item.symbol)
    try {
      await marketApi.removeSymbolEverywhere(item.symbol)
      await loadWatchlists()
      setMessage(`已从自选和所有分组删除 ${item.name}（${item.symbol}）`)
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : `从${group.name}删除失败`)
    } finally {
      setRemovingSymbol(null)
    }
  }

  async function logout() {
    try { await marketApi.logout() } catch { /* Session may already be gone. */ }
    socket.current?.close()
    setUser(null)
    setAuth('anonymous')
  }

  if (auth === 'loading') return <main className="market-loading"><div className="market-brand-mark">顺</div><p>正在连接行情中心…</p></main>
  if (auth === 'anonymous') return <Login onLogin={(current) => { setUser(current); setAuth('authenticated'); if (!current.must_change_password) void loadWatchlists() }} />
  if (user?.must_change_password) return <PasswordChange user={user} onDone={(current) => { setUser(current); void loadWatchlists() }} />

  return <main className="market-app-shell">
    <header className="market-topbar">
      <a href="/market" className="market-wordmark"><span>顺</span><strong>行情中心</strong></a>
      <div className="market-search"><form onSubmit={addSymbol}><label className="sr-only" htmlFor="market-symbol">股票代码</label><input id="market-symbol" inputMode="numeric" placeholder="输入 6 位代码添加自选" value={symbol} onChange={(event) => setSymbol(event.target.value.replace(/\D/g, '').slice(0, 6))} /><button type="submit" disabled={symbol.length !== 6}>添加</button></form></div>
      <div className="market-account"><span><i className={connection} />{connection === 'live' ? '实时连接' : connection === 'reconnecting' ? '正在重连' : '轮询模式'}</span><button type="button" onClick={logout}>{user?.username} · 退出</button></div>
    </header>
    <div className="market-workspace">
      <aside className="market-watchlist">
        <div className="market-watchlist-head"><div><span>WATCHLIST</span><h2>我的自选</h2></div><button type="button" onClick={createGroup} aria-label="新建自选分组" title="添加分组">＋</button></div>
        <nav className="market-group-tabs" aria-label="自选分组">{groups.map((group) => <button type="button" key={group.id} className={activeGroup?.id === group.id ? 'active' : ''} onClick={() => setGroupId(group.id)}>{group.name}<small>{group.items.length}</small></button>)}</nav>
        <div className="market-list-label"><span>名称 / 代码</span><span>最新 / 涨幅</span></div>
        <div className="market-symbol-list">{activeGroup?.items.length ? activeGroup.items.map((item) => {
          const snapshot = snapshots[item.symbol]
          const removing = removingSymbol === item.symbol
          return <div key={item.symbol} className={`market-symbol-row${selected === item.symbol ? ' active' : ''}`}>
            <button type="button" className="market-symbol-select" onClick={() => { setSelected(item.symbol); setPeriod('timeshare') }} aria-label={`${item.name} ${item.symbol}`}>
              <span><strong className="market-symbol-name">{item.name}</strong><small>{item.symbol}</small></span><span className={`market-number-${tone(snapshot?.quote.change_percent)}`}><strong>{display(snapshot?.quote.price)}</strong><small>{display(snapshot?.quote.change_percent)}</small></span>
            </button>
            <button
              type="button"
              className="market-symbol-remove"
              disabled={removing}
              onClick={() => void removeSymbol(item, activeGroup)}
              aria-label={`删除 ${item.symbol}（从${activeGroup.name}和所有分组）`}
            >{removing ? '删除中' : '删除'}</button>
          </div>
        }) : <div className="market-watchlist-empty"><strong>还没有自选股</strong><span>在顶部输入代码，股票会先通过 App 精确确认。</span></div>}</div>
        <footer>每位用户最多 50 只自选股</footer>
      </aside>
      <div className="market-main-pane">
        {message && <div className="market-message" role="status">{message}<button type="button" onClick={() => setMessage('')}>关闭</button></div>}
        {selectedItem ? <Detail key={selectedItem.symbol} item={selectedItem} snapshot={snapshots[selectedItem.symbol]} period={period} setPeriod={setPeriod} series={period === 'day' ? dailySeries[selectedItem.symbol] : legacySeries} dailyLoading={dailyLoading[selectedItem.symbol] === true} dailyError={dailyErrors[selectedItem.symbol]} onRetryDaily={() => retryDailySeries(selectedItem.symbol)} /> : <section className="market-no-selection"><div className="market-brand-mark">顺</div><h1>从自选中选择一只股票</h1><p>行情会通过当前登录的同花顺 App 内部接口动态更新。</p></section>}
      </div>
    </div>
  </main>
}
