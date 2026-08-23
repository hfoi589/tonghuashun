import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { IntradayMetricChart } from './IntradayMetricChart'
import { intradayTimeRatio } from './intraday-axis'

afterEach(cleanup)

describe('IntradayMetricChart', () => {
  it('uses the fixed A-share trading-session axis instead of stretching point indices', () => {
    const { container } = render(<IntradayMetricChart
      directional
      series={{
        unit: null,
        points: [
          { time: '09:30', value: '0.10' },
          { time: '10:30', value: '0.20' },
          { time: '14:00', value: '0.30' },
        ],
      }}
      title="测试指标"
    />)

    expect(screen.getByText('09:30', { selector: '.intraday-x-axis-label' })).toBeInTheDocument()
    expect(screen.getByText('11:30/13:00', { selector: '.intraday-x-axis-label' })).toBeInTheDocument()
    expect(screen.getByText('15:00', { selector: '.intraday-x-axis-label' })).toBeInTheDocument()
    expect(container.querySelector('.chart-series-line')).toHaveAttribute(
      'd',
      expect.stringContaining('L 219.00'),
    )
    expect(container.querySelector('.chart-series-line')).toHaveAttribute(
      'd',
      expect.stringContaining('L 541.00'),
    )
  })

  it('rejects malformed wall-clock labels instead of folding them into lunch', () => {
    expect(intradayTimeRatio('10:99')).toBeNull()
    expect(intradayTimeRatio('12:00')).toBeNull()
    expect(intradayTimeRatio('08:59')).toBeNull()
  })

  it('follows a controlled market time without accepting an independent pointer selection', () => {
    const { rerender } = render(<IntradayMetricChart
      directional
      selectedTime="09:30"
      series={{ unit: null, points: [
        { time: '09:30', value: '0.10' },
        { time: '10:30', value: '0.20' },
      ] }}
      title="测试指标"
    />)

    const chart = screen.getByRole('img', { name: '测试指标当日分时图' })
    expect(chart).not.toHaveAttribute('tabindex')
    expect(screen.getByText('09:30', { selector: 'time' })).toBeInTheDocument()
    expect(screen.getByText('0.10', { selector: '.chart-readout strong' })).toBeInTheDocument()

    rerender(<IntradayMetricChart
      directional
      selectedTime="10:30"
      series={{ unit: null, points: [
        { time: '09:30', value: '0.10' },
        { time: '10:30', value: '0.20' },
      ] }}
      title="测试指标"
    />)

    expect(screen.getByText('10:30', { selector: 'time' })).toBeInTheDocument()
    expect(screen.getByText('0.20', { selector: '.chart-readout strong' })).toBeInTheDocument()
    expect(chart.querySelector('.chart-cursor-line')).toBeInTheDocument()
  })

  it('keeps the shared time and shows a gap when that metric has no exact point', () => {
    render(<IntradayMetricChart
      directional
      selectedTime="10:00"
      series={{ unit: null, points: [
        { time: '09:30', value: '0.10' },
        { time: '10:30', value: '0.20' },
      ] }}
      title="测试指标"
    />)

    expect(screen.getByText('10:00', { selector: 'time' })).toBeInTheDocument()
    expect(screen.getByText('—', { selector: '.chart-readout strong' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '测试指标当日分时图' }).querySelector('.chart-active-point')).not.toBeInTheDocument()
  })
})
