import { describe, expect, it, vi } from 'vitest'
import { DeviceInputAdapter } from './device-stream'

it('sends the documented input envelope only when stream and operator lock are ready', () => {
  const socket = { readyState: 1, send: vi.fn(), close: vi.fn() }
  const adapter = new DeviceInputAdapter(() => socket)

  expect(adapter.send({ ready: false, locked: true }, { kind: 'tap', x: 0.2, y: 0.4 })).toBe(false)
  expect(adapter.send({ ready: true, locked: false }, { kind: 'tap', x: 0.2, y: 0.4 })).toBe(false)
  expect(adapter.send({ ready: true, locked: true }, { kind: 'tap', x: 0.2, y: 0.4 })).toBe(true)
  expect(socket.send).toHaveBeenCalledWith(JSON.stringify({
    type: 'input',
    sequence: 1,
    event: { kind: 'tap', x: 0.2, y: 0.4 },
  }))
})
