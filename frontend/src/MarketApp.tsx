import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError } from './api'
import {
  marketApi,
  marketStreamUrl,
  type KlineBar,
  type MarketSeriesPage,
  type MarketSnapshot,
  type MarketUser,
  type TimesharePoint,
  type WatchlistGroup,
} from './market-api'
import './market.css'

type AuthState = 'loading' | 'anonymous' | 'authenticated'
type ChartPeriod = 'timeshare' | 'five_day' | 'day' | 'week' | 'month'

const chartPeriods: Array<[ChartPeriod, string]> = [
  ['timeshare', '分时'],
  ['five_day', '五日'],
  ['day', '日K'],
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

function LineChart({ name, points }: { name: string, points: TimesharePoint[] }) {
  const data = points
    .map((point, index) => ({ index, point, value: Number(point.price) }))
    .filter((item) => Number.isFinite(item.value))
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
  const x = (index: number) => pad.left + index / Math.max(1, points.length - 1) * (width - pad.left - pad.right)
  const y = (value: number) => pad.top + (max - value) / (max - min) * (height - pad.top - pad.bottom)
  const path = data.map((item, index) => `${index === 0 ? 'M' : 'L'}${x(item.index).toFixed(2)},${y(item.value).toFixed(2)}`).join(' ')
  const area = `${path} L${x(data.at(-1)!.index)},${height - pad.bottom} L${x(data[0].index)},${height - pad.bottom} Z`
  const ticks = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])]
  return <svg className="market-price-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${name}分时价格图`}>
    {[0, 1, 2, 3, 4].map((tick) => {
      const py = pad.top + tick / 4 * (height - pad.top - pad.bottom)
      const value = max - tick / 4 * (max - min)
      return <g key={tick}><line x1={pad.left} y1={py} x2={width - pad.right} y2={py} /><text x={pad.left - 9} y={py + 4} textAnchor="end">{value.toFixed(2)}</text></g>
    })}
    <path className="market-chart-area" d={area} />
    <path className="market-chart-line" d={path} />
    {ticks.map((index) => <text key={index} x={x(index)} y={height - 12} textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}>{points[index]?.time}</text>)}
  </svg>
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
  return <section className="market-section">
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

function Detail({ item, snapshot, period, setPeriod, series }: {
  item: { symbol: string, name: string },
  snapshot?: MarketSnapshot,
  period: ChartPeriod,
  setPeriod: (period: ChartPeriod) => void,
  series?: MarketSeriesPage,
}) {
  const quote = snapshot?.quote ?? {}
  const changeTone = tone(quote.change_percent)
  const klineAvailable = snapshot?.capabilities.kline?.available === true
  return <section className="market-detail">
    <header className="market-quote-head">
      <div><p>{item.symbol} · 实时行情</p><h1>{snapshot?.name ?? item.name}</h1></div>
      <div className={`market-last-price market-number-${changeTone}`}><strong>{display(quote.price)}</strong><span>{display(quote.change_percent)}</span></div>
      <div className="market-live-status"><i className={snapshot?.stale ? 'stale' : ''} />{snapshot?.source_time ? `${snapshot.source_time} 更新` : '正在读取 App 接口'}</div>
    </header>
    <div className="market-quote-strip">
      {[['今开', quote.open], ['最高', quote.high], ['最低', quote.low], ['昨收', quote.previous_close], ['换手', quote.turnover_rate], ['成交量', quote.volume]].map(([label, value]) => <div key={label}><span>{label}</span><strong>{display(value)}</strong></div>)}
    </div>
    <section className="market-chart-panel">
      <nav className="market-period-tabs" aria-label="图表周期">{chartPeriods.map(([value, label]) => <button type="button" key={value} className={period === value ? 'active' : ''} onClick={() => setPeriod(value)}>{label}</button>)}</nav>
      {period === 'timeshare'
        ? <LineChart name={snapshot?.name ?? item.name} points={snapshot?.timeshare ?? []} />
        : !klineAvailable
          ? <div className="market-capability-gap"><strong>App 内部 K 线接口尚未确认</strong><span>为避免展示错误行情，这里不会使用网页源、OCR 或猜测参数。</span></div>
          : <CandleChart name={snapshot?.name ?? item.name} bars={series?.bars ?? []} />}
    </section>
    <section className="market-metric-grid">
      {[
        ['大单净量', quote.large_order_net], ['大单金额', quote.large_order_amount], ['散户数量', quote.retail_count], ['MACDFS', quote.macdfs],
      ].map(([label, value]) => <article key={label}><span>{label}</span><strong className={`market-number-${tone(value)}`}>{display(value)}</strong></article>)}
    </section>
    {snapshot && <FundFlow values={snapshot.main_fund_flow} />}
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
  const [series, setSeries] = useState<MarketSeriesPage>()
  const [symbol, setSymbol] = useState('')
  const [message, setMessage] = useState('')
  const [connection, setConnection] = useState<'live' | 'reconnecting' | 'unavailable'>('unavailable')
  const socket = useRef<WebSocket | null>(null)

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
    setGroupId((current) => current ?? result.groups[0]?.id ?? null)
    setSelected((current) => current ?? result.groups[0]?.items[0]?.symbol ?? null)
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
    if (period === 'timeshare' || !selected || !selectedKlineAvailable) {
      setSeries(undefined)
      return
    }
    let active = true
    void marketApi.series(selected, period).then((value) => { if (active) setSeries(value) }).catch((reason) => { if (active) setMessage(reason instanceof Error ? reason.message : 'K 线读取失败') })
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
        <div className="market-watchlist-head"><div><span>WATCHLIST</span><h2>我的自选</h2></div><button type="button" onClick={createGroup} aria-label="新建自选分组">＋</button></div>
        <nav className="market-group-tabs" aria-label="自选分组">{groups.map((group) => <button type="button" key={group.id} className={activeGroup?.id === group.id ? 'active' : ''} onClick={() => setGroupId(group.id)}>{group.name}<small>{group.items.length}</small></button>)}</nav>
        <div className="market-list-label"><span>名称 / 代码</span><span>最新 / 涨幅</span></div>
        <div className="market-symbol-list">{activeGroup?.items.length ? activeGroup.items.map((item) => {
          const snapshot = snapshots[item.symbol]
          return <button type="button" key={item.symbol} className={selected === item.symbol ? 'active' : ''} onClick={() => { setSelected(item.symbol); setPeriod('timeshare') }} aria-label={`${item.name} ${item.symbol}`}>
            <span><strong>{item.name}</strong><small>{item.symbol}</small></span><span className={`market-number-${tone(snapshot?.quote.change_percent)}`}><strong>{display(snapshot?.quote.price)}</strong><small>{display(snapshot?.quote.change_percent)}</small></span>
          </button>
        }) : <div className="market-watchlist-empty"><strong>还没有自选股</strong><span>在顶部输入代码，股票会先通过 App 精确确认。</span></div>}</div>
        <footer>每位用户最多 50 只自选股</footer>
      </aside>
      <div className="market-main-pane">
        {message && <div className="market-message" role="status">{message}<button type="button" onClick={() => setMessage('')}>关闭</button></div>}
        {selectedItem ? <Detail item={selectedItem} snapshot={snapshots[selectedItem.symbol]} period={period} setPeriod={setPeriod} series={series} /> : <section className="market-no-selection"><div className="market-brand-mark">顺</div><h1>从自选中选择一只股票</h1><p>行情会通过当前登录的同花顺 App 内部接口动态更新。</p></section>}
      </div>
    </div>
  </main>
}
