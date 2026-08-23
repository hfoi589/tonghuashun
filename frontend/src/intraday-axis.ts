export const intradayAxisTicks = [
  { ratio: 0, label: '09:30', anchor: 'start' },
  { ratio: .5, label: '11:30/13:00', anchor: 'middle' },
  { ratio: 1, label: '15:00', anchor: 'end' },
] as const

export function intradayTimeRatio(time: string): number | null {
  const matched = /^(\d{2}):(\d{2})$/.exec(time.trim())
  if (!matched) return null
  const hour = Number(matched[1])
  const minute = Number(matched[2])
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null
  const total = hour * 60 + minute
  const morningStart = 9 * 60 + 30
  const morningEnd = 11 * 60 + 30
  const afternoonStart = 13 * 60
  const afternoonEnd = 15 * 60
  if (total < morningStart || total > afternoonEnd) return null
  if (total <= morningEnd) return (total - morningStart) / 240
  if (total < afternoonStart) return null
  return (120 + total - afternoonStart) / 240
}

export function intradayPointRatio(time: string, index: number, count: number): number {
  return intradayTimeRatio(time) ?? index / Math.max(1, count - 1)
}

export function nearestIntradayPointIndex(
  points: Array<{ time: string }>,
  targetRatio: number,
): number {
  let nearest = 0
  let distance = Number.POSITIVE_INFINITY
  points.forEach((point, index) => {
    const nextDistance = Math.abs(intradayPointRatio(point.time, index, points.length) - targetRatio)
    if (nextDistance < distance) {
      nearest = index
      distance = nextDistance
    }
  })
  return nearest
}
