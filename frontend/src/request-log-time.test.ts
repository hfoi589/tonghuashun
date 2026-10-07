import { describe, expect, it } from 'vitest'
import { beijingDateBoundaryToUtc, formatRequestLogTimestamp } from './request-log-time'

describe('request log Beijing time', () => {
  it('formats stored UTC timestamps as Beijing date and time', () => {
    expect(formatRequestLogTimestamp('2026-10-01T01:02:31+00:00')).toEqual({
      date: '2026-10-01',
      time: '09:02:31',
    })
  })

  it('converts selected Beijing day boundaries to UTC for filtering', () => {
    expect(beijingDateBoundaryToUtc('2026-10-01', 'start')).toBe(
      '2026-09-30T16:00:00+00:00',
    )
    expect(beijingDateBoundaryToUtc('2026-10-01', 'end')).toBe(
      '2026-10-01T15:59:59.999999+00:00',
    )
  })
})
