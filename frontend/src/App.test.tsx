import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { subscribeToJob, type Job } from './api'

const task: Job = {
  public_id: 'public-job-1',
  symbol: '600938',
  status: 'QUEUED',
  error_code: null,
  created_at: '2026-08-21T08:00:00+00:00',
  captures: [
    { kind: 'LARGE_ORDER_NET', status: 'PENDING', url: null, expires_at: null },
    { kind: 'LARGE_ORDER_AMOUNT', status: 'PENDING', url: null, expires_at: null },
    { kind: 'RETAIL_COUNT', status: 'PENDING', url: null, expires_at: null },
  ],
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('Level2 web UI', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    cleanup()
    window.history.replaceState({}, '', '/')
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('normalizes and submits an App-searchable symbol', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(task, 202))
    const user = userEvent.setup()

    render(<App />)
    await user.type(screen.getByLabelText('股票代码'), ' sz.000001 ')
    await user.click(screen.getByRole('button', { name: '提交截图任务' }))

    await waitFor(() => expect(screen.getByText('已进入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenCalledWith('/api/v1/jobs', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ symbol: 'SZ.000001' }),
    }))
    expect(new URL(window.location.href).searchParams.get('job')).toBe('public-job-1')
  })

  it('restores a public job from the URL after refresh', async () => {
    window.history.replaceState({}, '', '/?job=public-job-1')
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(task))

    render(<App />)

    await waitFor(() => expect(screen.getByText('已进入队列')).toBeInTheDocument())
    expect(fetch).toHaveBeenCalledWith('/api/v1/jobs/public-job-1', expect.anything())
  })

  it('shows ready and missing cards for a partial result', () => {
    render(<App initialTask={{
      ...task,
      status: 'PARTIAL',
      captures: [
        { kind: 'LARGE_ORDER_NET', status: 'READY', url: '/api/v1/jobs/public-job-1/captures/LARGE_ORDER_NET', expires_at: '2026-08-22T08:00:00+00:00' },
        task.captures[1],
        task.captures[2],
      ],
    }} />)

    expect(screen.getByText('部分截图已可用')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '打开截图' })).toHaveAttribute('href', '/api/v1/jobs/public-job-1/captures/LARGE_ORDER_NET')
    expect(screen.getAllByText('暂未生成')).toHaveLength(2)
  })

  it('renders an expired job without capture actions', () => {
    render(<App initialTask={{ ...task, status: 'EXPIRED' }} />)

    expect(screen.getByText('结果已过期')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '打开截图' })).not.toBeInTheDocument()
  })
})

describe('job SSE subscription', () => {
  class FakeEventSource {
    static instance: FakeEventSource | undefined
    closed = false
    onerror: ((event: Event) => void) | null = null
    private readonly listeners = new Map<string, (() => void)[]>()

    constructor(_url: string) { FakeEventSource.instance = this }
    addEventListener(name: string, listener: () => void) {
      this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener])
    }
    close() { this.closed = true }
    emitStatus() { this.listeners.get('status')?.forEach((listener) => listener()) }
  }

  afterEach(() => vi.unstubAllGlobals())

  it('keeps native EventSource reconnecting after a transient error', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const changed = vi.fn()
    const unsubscribe = subscribeToJob('public-job-1', changed)
    const stream = FakeEventSource.instance!

    stream.emitStatus()
    stream.onerror?.(new Event('error'))
    stream.emitStatus()

    expect(changed).toHaveBeenCalledTimes(2)
    expect(stream.closed).toBe(false)
    unsubscribe()
    expect(stream.closed).toBe(true)
  })
})
