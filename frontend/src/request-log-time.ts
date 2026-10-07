export type RequestLogBoundary = 'start' | 'end'

export function formatRequestLogTimestamp(timestamp: string): { date: string; time: string } {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(timestamp))
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? ''

  return {
    date: `${part('year')}-${part('month')}-${part('day')}`,
    time: `${part('hour')}:${part('minute')}:${part('second')}`,
  }
}

export function beijingDateBoundaryToUtc(date: string, boundary: RequestLogBoundary): string {
  const time = boundary === 'start' ? '00:00:00.000' : '23:59:59.999'
  const utc = new Date(`${date}T${time}+08:00`).toISOString()
  return boundary === 'start'
    ? utc.replace('.000Z', '+00:00')
    : utc.replace('.999Z', '.999999+00:00')
}
