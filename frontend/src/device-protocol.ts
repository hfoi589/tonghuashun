/**
 * Runner WebSocket protocol shared by this UI and the future Task 3 runner.
 * Frames are base64 JPEG payloads. Input coordinates are normalized to 0..1.
 */
export interface DeviceFrame {
  type: 'frame'
  encoding: 'jpeg'
  sequence: number
  capturedAt: string
  data: string
}

export interface RunnerStatus {
  type: 'runner_status'
  state: string
  locked: boolean
  sequence?: number
}

export type DeviceInputEvent =
  | { kind: 'tap'; x: number; y: number }
  | { kind: 'swipe'; startX: number; startY: number; endX: number; endY: number }
  | { kind: 'key'; key: string; action: 'down' | 'up' }

export interface DeviceInput {
  type: 'input'
  sequence: number
  event: DeviceInputEvent
}

export type DeviceServerMessage = DeviceFrame | RunnerStatus

export function parseDeviceServerMessage(value: unknown): DeviceServerMessage | undefined {
  if (!value || typeof value !== 'object') return undefined
  const message = value as Record<string, unknown>
  const { sequence } = message
  if (message.type === 'frame' && message.encoding === 'jpeg' && typeof sequence === 'number' && typeof message.capturedAt === 'string' && typeof message.data === 'string') {
    return { type: 'frame', encoding: 'jpeg', sequence, capturedAt: message.capturedAt, data: message.data }
  }
  if (message.type === 'runner_status' && typeof message.state === 'string' && typeof message.locked === 'boolean' && (sequence === undefined || typeof sequence === 'number')) {
    return sequence === undefined ? { type: 'runner_status', state: message.state, locked: message.locked } : { type: 'runner_status', state: message.state, locked: message.locked, sequence }
  }
  return undefined
}
