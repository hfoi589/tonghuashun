import { describe, expect, it } from 'vitest'
import { chartPeriods, isFundFlowPeriod } from './MarketApp'

describe('market dark-pool tab contract', () => {
  it('places 暗盘 first and keeps it as the default period', () => {
    expect(chartPeriods[0]).toEqual(['dark_pool', '暗盘'])
    expect(chartPeriods[1]).toEqual(['timeshare', '分时'])
  })

  it('shows fund flow only for the dark-pool period', () => {
    expect(isFundFlowPeriod('dark_pool')).toBe(true)
    expect(isFundFlowPeriod('timeshare')).toBe(false)
    expect(isFundFlowPeriod('day')).toBe(false)
  })
})
