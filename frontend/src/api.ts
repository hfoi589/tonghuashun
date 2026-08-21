export const captureKinds = ['LARGE_ORDER_NET', 'LARGE_ORDER_AMOUNT', 'RETAIL_COUNT'] as const
export type CaptureKind = (typeof captureKinds)[number]
export type CaptureStatus = 'PENDING' | 'READY' | 'EXPIRED'
export type JobStatus = 'QUEUED' | 'RUNNING' | 'WAITING_ADMIN' | 'COMPLETED' | 'PARTIAL' | 'FAILED' | 'EXPIRED'

export interface Capture {
  kind: CaptureKind
  status: CaptureStatus
  url: string | null
  expires_at: string | null
}

export interface Job {
  public_id: string
  symbol: string
  status: JobStatus
  error_code: string | null
  created_at: string
  captures: Capture[]
}

export interface RunnerHealth {
  state: string
  last_heartbeat: string | null
  queue_paused: boolean
}

export interface LockState {
  locked: boolean
}

export interface QueueState {
  paused: boolean
}

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: 'include', ...init })
  if (!response.ok) {
    let message = '请求未完成'
    try {
      const body = await response.json() as { detail?: string }
      message = body.detail ?? message
    } catch {
      // HTTP status is still a useful, safe failure signal.
    }
    throw new ApiError(response.status, message)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}

export const api = {
  submitJob: (symbol: string) => request<Job>('/api/v1/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol }),
  }),
  getJob: (publicId: string) => request<Job>(`/api/v1/jobs/${encodeURIComponent(publicId)}`),
  login: (password: string) => request<void>('/api/admin/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  }),
  runner: () => request<RunnerHealth>('/api/admin/runner'),
  lock: () => request<LockState>('/api/admin/lock'),
  queue: () => request<QueueState>('/api/admin/queue'),
  pauseQueue: (csrfToken: string) => request<QueueState>('/api/admin/queue/pause', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  resumeQueue: (csrfToken: string) => request<QueueState>('/api/admin/queue/resume', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  acquireLock: (csrfToken: string) => request<LockState>('/api/admin/lock/acquire', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
  releaseLock: (csrfToken: string) => request<LockState>('/api/admin/lock/release', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),
}

export type JobStreamState = 'CONNECTED' | 'RECONNECTING' | 'UNAVAILABLE'

export function subscribeToJob(publicId: string, onChange: () => void, onStateChange?: (state: JobStreamState) => void): () => void {
  if (typeof EventSource === 'undefined') return () => undefined
  const stream = new EventSource(`/api/v1/jobs/${encodeURIComponent(publicId)}/events`)
  stream.addEventListener('status', onChange)
  stream.onopen = () => onStateChange?.('CONNECTED')
  stream.onerror = () => onStateChange?.('RECONNECTING')
  return () => stream.close()
}

export function readCsrfToken(cookie = document.cookie): string {
  return cookie.split('; ').find((item) => item.startsWith('ths_csrf='))?.slice('ths_csrf='.length) ?? ''
}
