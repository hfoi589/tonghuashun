import type { DeviceInput, DeviceInputEvent } from './device-protocol'

export interface DeviceSocket {
  readyState: number
  send(data: string): void
  close(): void
}

export interface DeviceInputState {
  ready: boolean
  locked: boolean
}

/** Sends only sequenced, documented input envelopes to an open, authorized stream. */
export class DeviceInputAdapter {
  private sequence = 0

  constructor(private readonly socket: () => DeviceSocket | null) {}

  send(state: DeviceInputState, event: DeviceInputEvent): boolean {
    const socket = this.socket()
    if (!state.ready || !state.locked || socket?.readyState !== 1) return false
    const message: DeviceInput = { type: 'input', sequence: ++this.sequence, event }
    socket.send(JSON.stringify(message))
    return true
  }
}
