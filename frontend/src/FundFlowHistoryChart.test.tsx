import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FundFlowHistoryChart, fundFlowToWan, type FundFlowHistoryPeriod } from './FundFlowHistoryChart'
import { FundFlowDailyChart, FundFlowDailyPanel } from './FundFlowDailyChart'
import { marketApi } from './market-api'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const period: FundFlowHistoryPeriod = {
  points: [
    {
      time: '09:30',
      unit: '万元',
      main_net_inflow: '10000.00',
      main_visible_inflow: '8000.00',
      main_hidden_inflow: '2000.00',
      retail_inflow: '-10000.00',
    },
    {
      time: '10:00',
      unit: '万元',
      main_net_inflow: '12000.00',
      main_visible_inflow: '9000.00',
      main_hidden_inflow: '3000.00',
      retail_inflow: '-12000.00',
    },
  ],
}

describe('FundFlowHistoryChart', () => {
  it('converts both supported source units to 万元', () => {
    expect(fundFlowToWan('1234.56', '万元')).toBe(1234.56)
    expect(fundFlowToWan('1.23', '亿元')).toBe(12300)
    expect(fundFlowToWan('unknown', '万元')).toBeNull()
  })

  it('renders four colored series normalized to 万元', () => {
    const { container } = render(<FundFlowHistoryChart period={period} />)

    expect(screen.getAllByText('主力明盘').length).toBeGreaterThan(0)
    expect(screen.getAllByText('主力暗盘').length).toBeGreaterThan(0)
    expect(screen.getAllByText('散户流入').length).toBeGreaterThan(0)
    expect(screen.getByTestId('fund-flow-readout-main-visible')).toHaveTextContent('9000.00')
    expect(screen.getByTestId('fund-flow-readout-main-hidden')).toHaveTextContent('3000.00')
    expect(screen.getByTestId('fund-flow-readout-retail')).toHaveTextContent('-12000.00')
    expect(screen.getByTestId('fund-flow-readout-main-net')).toHaveTextContent('12000.00')
    expect(container.querySelectorAll('.fund-flow-history-readout em')).toHaveLength(0)
    expect(container.querySelectorAll('.fund-flow-history-series')).toHaveLength(4)
    expect(container.querySelector('.fund-flow-history-series-visible')).toHaveStyle({ stroke: '#d92d20' })
    expect(container.querySelector('.fund-flow-history-series-hidden')).toHaveStyle({ stroke: '#7f1d1d' })
    expect(container.querySelector('.fund-flow-history-series-retail')).toHaveStyle({ stroke: '#16803c' })
    expect(container.querySelector('.fund-flow-history-series-net')).toHaveStyle({ stroke: '#b7791f' })
    expect([...container.querySelectorAll('.fund-flow-history-legend span')].map((node) => node.textContent)).toEqual(['散户流入', '主力明盘', '主力暗盘', '主力净流入'])
    expect(container.querySelector('.fund-flow-history-series-visible')?.getAttribute('d')).toContain(' L')
  })

  it('renders a capability gap when the selected period has no points', () => {
    render(<FundFlowHistoryChart period={{ points: [] }} />)
    expect(screen.getByText('暂无该周期资金流历史')).toBeInTheDocument()
  })

  it('skips lunch-break points and connects morning to afternoon directly', () => {
    const { container } = render(<FundFlowHistoryChart period={{ points: [
      { ...period.points[0], time: '11:30' },
      { ...period.points[0], time: '12:00', main_visible_inflow: '99999.00' },
      { ...period.points[1], time: '13:00' },
    ] }} />)

    const path = container.querySelector('.fund-flow-history-series-visible')?.getAttribute('d') ?? ''
    expect(path.match(/L/g)?.length).toBe(1)
    expect(path).not.toContain('99999')
  })

  it('keeps active values in the top readout and updates them with the scrubber', () => {
    const { container } = render(<FundFlowHistoryChart period={period} />)

    expect(container.querySelectorAll('.fund-flow-history-active-label')).toHaveLength(0)
    expect(container.querySelector('[data-testid="fund-flow-readout-main-visible"]')).toHaveTextContent('9000.00')
    expect(container.querySelector('[data-testid="fund-flow-readout-main-hidden"]')).toHaveTextContent('3000.00')
    expect(container.querySelector('[data-testid="fund-flow-readout-retail"]')).toHaveTextContent('-12000.00')
    expect(container.querySelector('[data-testid="fund-flow-readout-main-net"]')).toHaveTextContent('12000.00')

    fireEvent.change(container.querySelector('.fund-flow-history-scrubber')!, { target: { value: '0' } })

    expect(screen.getByText('09:30', { selector: 'time' })).toBeInTheDocument()
    expect(container.querySelector('[data-testid="fund-flow-readout-main-visible"]')).toHaveTextContent('8000.00')
    expect(container.querySelector('[data-testid="fund-flow-readout-main-hidden"]')).toHaveTextContent('2000.00')
    expect(container.querySelector('[data-testid="fund-flow-readout-retail"]')).toHaveTextContent('-10000.00')
    expect(container.querySelector('[data-testid="fund-flow-readout-main-net"]')).toHaveTextContent('10000.00')
  })

  it('uses semantic colors in the legend for each series', () => {
    const { container } = render(<FundFlowHistoryChart period={period} />)

    const legend = [...container.querySelectorAll('.fund-flow-history-legend span')]
    expect(legend[0]?.querySelector('i')).toHaveStyle({ backgroundColor: '#16803c' })
    expect(legend[1]?.querySelector('i')).toHaveStyle({ backgroundColor: '#d92d20' })
    expect(legend[2]?.querySelector('i')).toHaveStyle({ backgroundColor: '#7f1d1d' })
    expect(legend[3]?.querySelector('i')).toHaveStyle({ backgroundColor: '#b7791f' })
  })

  it('keeps main net inflow from its authoritative field when a component is missing', () => {
    const { container } = render(<FundFlowHistoryChart period={{ points: [
      { ...period.points[0], main_hidden_inflow: null },
    ] }} />)

    expect(container.querySelectorAll('.fund-flow-history-active-label')).toHaveLength(0)
    expect(container.querySelector('[data-testid="fund-flow-readout-main-net"]')).toHaveTextContent('10000.00')
  })
})

describe('FundFlowDailyChart', () => {
  it('uses semantic readout colors and keeps 30-day axis labels readable', () => {
    const { container, unmount } = render(<FundFlowDailyChart points={Array.from({ length: 30 }, (_, index) => ({
      trade_date: `202609${String(index + 1).padStart(2, '0')}`,
      time: '15:00',
      unit: '万元',
      main_net_inflow: '10',
      main_visible_inflow: '6',
      main_hidden_inflow: '4',
      retail_inflow: '-10',
    }))} />)

    expect(container.querySelector('.fund-flow-daily-value-retail')).toBeInTheDocument()
    expect(container.querySelector('.fund-flow-daily-value-visible')).toBeInTheDocument()
    expect(container.querySelector('.fund-flow-daily-value-hidden')).toBeInTheDocument()
    expect(container.querySelector('.fund-flow-daily-value-net')).toBeInTheDocument()
    expect(container.querySelectorAll('.fund-flow-daily-readout em')).toHaveLength(0)
    expect(container.querySelectorAll('.fund-flow-daily-svg > text')).toHaveLength(6)
    unmount()
  })

  it('shows the hovered trade date values without repeating the single-day legend', () => {
    render(<FundFlowDailyChart points={[
      { trade_date: '20260914', time: '00:00', unit: '万元', main_net_inflow: '0', main_visible_inflow: '0', main_hidden_inflow: '0', retail_inflow: '0' },
      { trade_date: '20260915', time: '15:00', unit: '万元', main_net_inflow: '8', main_visible_inflow: '5', main_hidden_inflow: '3', retail_inflow: '-8' },
      { trade_date: '20260916', time: '15:00', unit: '万元', main_net_inflow: '10', main_visible_inflow: '6', main_hidden_inflow: '4', retail_inflow: '-10' },
    ]} />)

    const chart = screen.getByRole('img', { name: '最近30个交易日资金流向折线图' })
    Object.defineProperty(chart, 'getBoundingClientRect', {
      value: () => ({ left: 0, right: 900, top: 0, bottom: 320, width: 900, height: 320, x: 0, y: 0, toJSON: () => ({}) }),
    })
    expect(document.querySelector('.fund-flow-daily-chart .fund-flow-history-legend')).not.toBeInTheDocument()
    expect(screen.getByText('20260916', { selector: 'time' })).toBeInTheDocument()

    fireEvent.pointerMove(chart, { clientX: 450 })

    expect(screen.getByText('20260915', { selector: 'time' })).toBeInTheDocument()
    expect(screen.getByText('8.00', { selector: 'strong' })).toBeInTheDocument()
    expect(screen.getAllByText('09-14').length).toBeGreaterThan(0)
  })

  it('passes the selected raw daily point and periods through the Market panel', async () => {
    const onSelectedPointChange = vi.fn()
    vi.spyOn(marketApi, 'fundFlowDaily').mockResolvedValue({
      symbol: '601872',
      name: '招商轮船',
      limit: 30,
      points: [{
        trade_date: '20260915',
        time: '15:00',
        unit: '万元',
        main_net_inflow: '8',
        main_visible_inflow: '5',
        main_hidden_inflow: '3',
        retail_inflow: '-8',
        periods: {
          today: { unit: '万元', main_net_inflow: '801', main_visible_inflow: '81', main_hidden_inflow: '82', retail_inflow: '-801' },
          three_day: { unit: '万元', main_net_inflow: '802', main_visible_inflow: '83', main_hidden_inflow: '84', retail_inflow: '-802' },
          five_day: { unit: '万元', main_net_inflow: '805', main_visible_inflow: '85', main_hidden_inflow: '86', retail_inflow: '-805' },
        },
      }],
    })

    const DailyPanel = FundFlowDailyPanel as unknown as (props: { symbol: string, onSelectedPointChange: (point: unknown) => void }) => ReactElement
    render(<DailyPanel symbol="601872" onSelectedPointChange={onSelectedPointChange} />)

    await waitFor(() => expect(onSelectedPointChange).toHaveBeenLastCalledWith(expect.objectContaining({
      trade_date: '20260915',
      periods: expect.objectContaining({ today: expect.objectContaining({ main_net_inflow: '801' }) }),
    })))
  })

  it('refreshes the Market daily panel on the same 30-second cadence as history', async () => {
    vi.useFakeTimers()
    const response = {
      symbol: '601872',
      name: '招商轮船',
      limit: 30,
      points: [{ trade_date: '20260915', time: '15:00', unit: '万元', main_net_inflow: '8', main_visible_inflow: '5', main_hidden_inflow: '3', retail_inflow: '-8', periods: null }],
    }
    const request = vi.spyOn(marketApi, 'fundFlowDaily').mockResolvedValue(response)
    render(<FundFlowDailyPanel symbol="601872" pollingEnabled />)
    expect(request).toHaveBeenCalledTimes(1)

    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(request).toHaveBeenCalledTimes(2)
  })
})
