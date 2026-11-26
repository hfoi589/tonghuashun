import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EtfFundFlowChartPanel, EtfHoldingsTablePanel } from './EtfFundFlowPanel'
import type { EtfFundFlowResponse } from './api'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const sampleEtfData: EtfFundFlowResponse = {
  symbol: '601872',
  name: '招商轮船',
  market: '17',
  period: '60d',
  available: true,
  summary: {
    latest_badge: {
      date: '11-24',
      full_date: '2026-11-24',
      value_text: '-1933.76万',
      text: '11-24 净流入 -1933.76万',
      tone: 'down',
    },
    metrics: [
      { key: '60d', label: '60日净流入', value_text: '8.16亿', tone: 'up' },
      { key: '20d', label: '20日净流入', value_text: '3.97亿', tone: 'up' },
      { key: '5d', label: '5日净流入', value_text: '1.10亿', tone: 'up' },
      { key: '3d', label: '3日净流入', value_text: '3810.92万', tone: 'up' },
    ],
  },
  chart: {
    cumulative_unit: '亿',
    daily_unit: '亿',
    points: [
      {
        date: '2026-08-25',
        short_date: '08-25',
        cumulative_value: 0.08,
        cumulative_text: '756.83万',
        daily_value: 0.08,
        daily_text: '756.83万',
        close_price: 16.50,
        price_change: null,
      },
      {
        date: '2026-10-14',
        short_date: '10-14',
        cumulative_value: 3.12,
        cumulative_text: '3.12亿',
        daily_value: 0.85,
        daily_text: '8500.00万',
        close_price: 21.10,
        price_change: 0.45,
      },
      {
        date: '2026-11-24',
        short_date: '11-24',
        cumulative_value: 8.16,
        cumulative_text: '8.16亿',
        daily_value: -0.19,
        daily_text: '-1933.76万',
        close_price: 15.53,
        price_change: 0.10,
      },
    ],
  },
  holdings: {
    total_count: 6,
    items: [
      { symbol: '510330', market: '20', name: '沪深300ETF华夏', net_inflow_raw: 6304019.53, net_inflow_text: '630.4万', tone: 'up', streak_days: 7, hold_rate: 0.25, hold_rate_text: '0.25%' },
      { symbol: '560700', market: '20', name: '央企红利ETF广发', net_inflow_raw: 986443.75, net_inflow_text: '98.64万', tone: 'up', streak_days: 5, hold_rate: 3.12, hold_rate_text: '3.12%' },
      { symbol: '561360', market: '20', name: '石油ETF国泰', net_inflow_raw: 512399.72, net_inflow_text: '51.24万', tone: 'up', streak_days: 1, hold_rate: 8.05, hold_rate_text: '8.05%' },
      { symbol: '159352', market: '36', name: 'A500ETF南方', net_inflow_raw: 124455.47, net_inflow_text: '12.45万', tone: 'up', streak_days: 2, hold_rate: 0.20, hold_rate_text: '0.20%' },
      { symbol: '561570', market: '20', name: '油气ETF华泰柏瑞', net_inflow_raw: 96350.47, net_inflow_text: '9.64万', tone: 'up', streak_days: 1, hold_rate: 8.00, hold_rate_text: '8.00%' },
      { symbol: '159300', market: '36', name: '沪深300ETF富国', net_inflow_raw: 84138.60, net_inflow_text: '8.41万', tone: 'up', streak_days: 3, hold_rate: 0.24, hold_rate_text: '0.24%' },
    ],
  },
}

describe('EtfFundFlowPanel', () => {
  it('renders ETF fund chart, summary cards, crosshair readout, and period switch', async () => {
    const user = userEvent.setup()
    const onPeriodChange = vi.fn()
    const onOpenHoldings = vi.fn()

    render(
      <EtfFundFlowChartPanel
        symbol="601872"
        market="17"
        data={sampleEtfData}
        period="60d"
        loading={false}
        onPeriodChange={onPeriodChange}
        onRetry={vi.fn()}
        onOpenHoldings={onOpenHoldings}
      />,
    )

    expect(screen.getByText('11-24 净流入 -1933.76万 ↓')).toBeInTheDocument()
    expect(screen.getByText('60日净流入')).toBeInTheDocument()
    expect(screen.getAllByText('8.16亿').length).toBeGreaterThan(0)
    expect(screen.getByText('3810.92万')).toBeInTheDocument()
    expect(screen.getByText('2026-11-24')).toBeInTheDocument()

    const svg = screen.getByRole('img', { name: 'ETF累计申购净流入与收盘价双轴走势图' })
    fireEvent.keyDown(svg, { key: 'ArrowLeft' })
    expect(screen.getByText('2026-10-14')).toBeInTheDocument()
    expect(screen.getByText('3.12亿')).toBeInTheDocument()
    expect(screen.getByText('21.10')).toBeInTheDocument()

    await user.selectOptions(screen.getByRole('combobox', { name: '选择ETF资金区间' }), '120d')
    expect(onPeriodChange).toHaveBeenCalledWith('120d')

    await user.click(screen.getByRole('button', { name: '明细 >' }))
    expect(onOpenHoldings).toHaveBeenCalledTimes(1)
  })

  it('renders top 5 holdings, supports column sorting, and expands with 更多 >', async () => {
    const user = userEvent.setup()
    render(
      <EtfHoldingsTablePanel
        symbol="601872"
        market="17"
        data={sampleEtfData}
        loading={false}
        onRetry={vi.fn()}
      />,
    )

    const table = screen.getByRole('table')
    expect(within(table).getAllByRole('row')).toHaveLength(6) // 1 header + 5 rows
    expect(screen.queryByText('沪深300ETF富国')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '按持仓占比排序' }))
    const rowsAfterSort = within(table).getAllByRole('row')
    expect(within(rowsAfterSort[1]).getByText('石油ETF国泰')).toBeInTheDocument()
    expect(within(rowsAfterSort[1]).getByText('8.05%')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '按持仓占比排序' }))
    const rowsAfterAsc = within(table).getAllByRole('row')
    expect(within(rowsAfterAsc[1]).getByText('A500ETF南方')).toBeInTheDocument()
    expect(within(rowsAfterAsc[1]).getByText('0.20%')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '更多 >' }))
    expect(within(table).getAllByRole('row')).toHaveLength(7)
    expect(screen.getByText('沪深300ETF富国')).toBeInTheDocument()
  })

  it('shows stale fallback banner, morning pending footnote, and partial holdings retry state', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    render(
      <EtfFundFlowChartPanel
        symbol="601872"
        market="17"
        data={{
          ...sampleEtfData,
          data_date: '2026-11-24',
          stale: true,
          update_status: 'stale_fallback',
        }}
        period="60d"
        loading={false}
        onPeriodChange={vi.fn()}
        onRetry={onRetry}
      />,
    )

    expect(screen.getByText(/已启用本地缓存兜底/)).toBeInTheDocument()
    expect(screen.getByText(/截至 2026-11-24/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '刷新' }))
    expect(onRetry).toHaveBeenCalledTimes(1)

    cleanup()
    render(
      <EtfFundFlowChartPanel
        symbol="601872"
        market="17"
        data={{
          ...sampleEtfData,
          data_date: '2026-11-23',
          stale: false,
          update_status: 'pending_morning_update',
        }}
        period="60d"
        loading={false}
        onPeriodChange={vi.fn()}
        onRetry={onRetry}
      />,
    )
    expect(screen.getByText(/上一交易日数据更新中/)).toBeInTheDocument()

    cleanup()
    render(
      <EtfHoldingsTablePanel
        symbol="601872"
        market="17"
        data={{
          ...sampleEtfData,
          degraded: true,
          update_status: 'partial',
          source_errors: {
            summary: null,
            line: null,
            holdings: 'ETF_FUND_FLOW_UNAVAILABLE',
            daily_kline: null,
          },
          holdings: { total_count: 0, items: [] },
        }}
        loading={false}
        onRetry={onRetry}
      />,
    )
    expect(screen.getByText('ETF申购明细暂不可用')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新加载' }))
    expect(onRetry).toHaveBeenCalledTimes(2)
  })
})
