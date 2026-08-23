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
  state: RunnerState
  locked: boolean
  sequence?: number
}

export interface DeviceStatus {
  type: 'device_status'
  role: 'core_metrics' | 'main_fund_flow'
  label: string
  adb: 'ONLINE' | 'OFFLINE' | 'UNKNOWN'
  app: 'ONLINE' | 'OFFLINE' | 'UNKNOWN'
  frida: 'ONLINE' | 'OFFLINE' | 'UNKNOWN'
}

export type RunnerState = 'BOOTING' | 'READY' | 'ADMIN_CONTROL' | 'NEEDS_ADMIN' | 'OFFLINE'
const runnerStates: ReadonlySet<string> = new Set(['BOOTING', 'READY', 'ADMIN_CONTROL', 'NEEDS_ADMIN', 'OFFLINE'])

export type DeviceInputEvent =
  | { kind: 'tap'; x: number; y: number }
  | { kind: 'swipe'; startX: number; startY: number; endX: number; endY: number }
  | { kind: 'key'; key: string; action: 'down' | 'up' }

export interface DeviceInput {
  type: 'input'
  sequence: number
  event: DeviceInputEvent
}

export type DeviceServerMessage = DeviceFrame | RunnerStatus | DeviceStatus

export function parseDeviceServerMessage(value: unknown): DeviceServerMessage | undefined {
  if (!value || typeof value !== 'object') return undefined
  const message = value as Record<string, unknown>
  const { sequence } = message
  if (message.type === 'frame' && message.encoding === 'jpeg' && typeof sequence === 'number' && typeof message.capturedAt === 'string' && typeof message.data === 'string') {
    return { type: 'frame', encoding: 'jpeg', sequence, capturedAt: message.capturedAt, data: message.data }
  }
  if (message.type === 'runner_status' && typeof message.state === 'string' && runnerStates.has(message.state) && typeof message.locked === 'boolean' && (sequence === undefined || typeof sequence === 'number')) {
    const state = message.state as RunnerState
    return sequence === undefined ? { type: 'runner_status', state, locked: message.locked } : { type: 'runner_status', state, locked: message.locked, sequence }
  }
  const healthStates = new Set(['ONLINE', 'OFFLINE', 'UNKNOWN'])
  if (
    message.type === 'device_status'
    && (message.role === 'core_metrics' || message.role === 'main_fund_flow')
    && typeof message.label === 'string'
    && typeof message.adb === 'string' && healthStates.has(message.adb)
    && typeof message.app === 'string' && healthStates.has(message.app)
    && typeof message.frida === 'string' && healthStates.has(message.frida)
  ) {
    return {
      type: 'device_status',
      role: message.role,
      label: message.label,
      adb: message.adb as DeviceStatus['adb'],
      app: message.app as DeviceStatus['app'],
      frida: message.frida as DeviceStatus['frida'],
    }
  }
  return undefined
}
