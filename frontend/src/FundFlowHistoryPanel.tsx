import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError } from './api'
import { FundFlowHistoryChart } from './FundFlowHistoryChart'
import { marketApi, type FundFlowHistoryResponse } from './market-api'

type FundFlowPeriodKey = 'today' | 'three_day' | 'five_day'

export function fundFlowHistoryPollIntervalMs(hasHistory: boolean): number {
  return hasHistory ? 30_000 : 5_000
}

const periods: Array<[FundFlowPeriodKey, string]> = [
  ['today', '当日'],
  ['three_day', '3日'],
  ['five_day', '5日'],
]

function dateLabel(value: string): string {
  return value.length === 8 ? `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6)}` : value
}

const FUND_SYMBOL_PREFIXES = [
  '158', '159', '160', '161', '162', '163', '164', '165', '166', '167', '168', '169', '180',
  '501', '502', '506', '508', '510', '511', '512', '513', '515', '516', '517', '518', '519',
  '520', '526', '530', '551', '560', '561', '562', '563', '588', '589',
]

export function isUnsupportedFundSymbol(symbol: string, market?: string): boolean {
  if (market === '20' || market === '36') return true
  const clean = symbol.trim()
  return /^\d{6}$/.test(clean) && FUND_SYMBOL_PREFIXES.some((prefix) => clean.startsWith(prefix))
}

export function FundFlowHistoryPanel({ symbol, market, pollingEnabled = true, onHistoryChange }: {
  symbol: string,
  market?: string,
  pollingEnabled?: boolean,
  onHistoryChange?: (history: FundFlowHistoryResponse | undefined) => void,
}) {
  const unsupportedFund = isUnsupportedFundSymbol(symbol, market)
  const [history, setHistory] = useState<FundFlowHistoryResponse>()
  const [selectedDate, setSelectedDate] = useState<string>()
  const [period, setPeriod] = useState<FundFlowPeriodKey>('today')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>()

  const load = useCallback(async (tradeDate?: string) => {
    if (unsupportedFund) return
    setLoading(true)
    setError(undefined)
    try {
      const next = await marketApi.fundFlowHistory(symbol, tradeDate)
      setHistory(next)
      setSelectedDate(next.trade_date ?? undefined)
      onHistoryChange?.(next)
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setHistory(undefined)
        setSelectedDate(undefined)
        onHistoryChange?.(undefined)
        setError(undefined)
      } else {
        setError(reason instanceof Error ? reason.message : '资金流历史读取失败')
      }
    } finally {
      setLoading(false)
    }
  }, [onHistoryChange, symbol, unsupportedFund])

  useEffect(() => {
    setHistory(undefined)
    setSelectedDate(undefined)
    setPeriod('today')
    onHistoryChange?.(undefined)
    if (!unsupportedFund) void load()
  }, [load, onHistoryChange, unsupportedFund])

  const latestDate = history?.available_dates[0]
  const followsLatest = selectedDate === undefined || selectedDate === latestDate
  useEffect(() => {
    if (unsupportedFund || !pollingEnabled || !followsLatest) return
    const timer = window.setInterval(
      () => { void load(history?.trade_date ?? undefined) },
      fundFlowHistoryPollIntervalMs(Boolean(history?.trade_date)),
    )
    return () => window.clearInterval(timer)
  }, [followsLatest, history?.trade_date, load, pollingEnabled, unsupportedFund])

  const selectedPeriod = history?.periods[period]
  const hasHistory = Boolean(history?.trade_date && history.available_dates.length)
  const periodLabel = useMemo(() => periods.find(([key]) => key === period)?.[1] ?? '当日', [period])

  if (unsupportedFund) {
    return <section className="fund-flow-history-panel" aria-label="主力流向历史">
      <div className="fund-flow-history-toolbar"><div><span>历史折线</span><small>能力说明</small></div></div>
      <p className="fund-flow-history-empty">交易所基金（ETF/LOF）无同花顺主力与暗盘资金统计，已跳过资金账号轮询。</p>
    </section>
  }

  return <section className="fund-flow-history-panel" aria-label="主力流向历史">
    {hasHistory ? <div className="fund-flow-history-toolbar fund-flow-history-toolbar-merged">
      <div className="fund-flow-history-periods" role="tablist" aria-label="资金流周期">
        {periods.map(([value, label]) => <button type="button" role="tab" aria-selected={period === value} className={period === value ? 'active' : ''} key={value} onClick={() => setPeriod(value)}>{label}</button>)}
      </div>
      <div className="fund-flow-history-meta-right">
        <small>{loading ? '正在更新…' : `单位：万元 · ${periodLabel}`}</small>
        <label><span className="sr-only">交易日</span><select value={selectedDate ?? ''} onChange={(event) => { setSelectedDate(event.target.value); void load(event.target.value) }} aria-label="选择资金流交易日">
          {history?.available_dates.map((date) => <option key={date} value={date}>{dateLabel(date)}</option>)}
        </select></label>
      </div>
    </div> : <div className="fund-flow-history-toolbar">
      <div><span>历史折线</span><small>{loading ? '正在更新…' : '暂无历史数据'}</small></div>
    </div>}
    {error && <p className="market-capability-gap"><strong>资金流历史加载失败</strong><span>{error}</span><button type="button" onClick={() => void load(selectedDate)}>重新加载</button></p>}
    {!error && hasHistory && selectedPeriod && <FundFlowHistoryChart period={selectedPeriod} tradeDate={history?.trade_date} />}
    {!error && !hasHistory && <p className="fund-flow-history-empty">定期刷新后，这里会按交易日累积明盘、暗盘、散户流入和主力净流入曲线。</p>}
  </section>
}
