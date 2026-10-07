import { describe, expect, it } from 'vitest'
import { fundFlowHistoryPollIntervalMs } from './FundFlowHistoryPanel'

describe('fund flow history polling', () => {
  it('retries quickly while the first current-day point is not available', () => {
    expect(fundFlowHistoryPollIntervalMs(false)).toBe(5_000)
    expect(fundFlowHistoryPollIntervalMs(true)).toBe(30_000)
  })
})
