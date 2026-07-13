import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  activeGateway,
  closeSecondaryGateways,
  ensureGatewayForProfile,
  setPrimaryGateway
} from './gateway'

type Listener = (event: unknown) => void

class FakeWebSocket {
  static OPEN = 1
  static CLOSED = 3
  static rpcMethods: string[] = []

  readyState = 0
  private listeners: Record<string, Set<Listener>> = {}

  constructor(public url: string) {
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN
      this.emit('open', {})
    })
  }

  addEventListener(type: string, listener: Listener) {
    ;(this.listeners[type] ??= new Set()).add(listener)
  }

  removeEventListener(type: string, listener: Listener) {
    this.listeners[type]?.delete(listener)
  }

  send(raw: string) {
    const frame = JSON.parse(raw) as { id?: string | number; method?: string }
    FakeWebSocket.rpcMethods.push(frame.method ?? '')
    this.emit('message', {
      data: JSON.stringify({ jsonrpc: '2.0', id: frame.id, result: { requests: [] } })
    })
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close', {})
  }

  private emit(type: string, event: unknown) {
    for (const listener of this.listeners[type] ?? []) {
      listener(event)
    }
  }
}

const originalWebSocket = globalThis.WebSocket

beforeEach(() => {
  FakeWebSocket.rpcMethods = []
  ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
    getConnection: vi.fn(async (profile?: string) => ({
      authMode: 'token',
      baseUrl: `https://${profile}.example.com`,
      profile,
      token: 'token',
      wsUrl: `wss://${profile}.example.com/api/ws?token=token`
    })),
    getGatewayWsUrl: vi.fn(async (connection: { wsUrl: string }) => connection.wsUrl),
    touchBackend: vi.fn(async () => undefined)
  }
  setPrimaryGateway(null, 'default')
})

afterEach(async () => {
  closeSecondaryGateways()
  setPrimaryGateway(null, 'default')
  await ensureGatewayForProfile('default')
  ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

describe('secondary profile gateway', () => {
  it('rehydrates pending clarify requests immediately after connecting', async () => {
    await ensureGatewayForProfile('research')

    expect(activeGateway()?.connectionState).toBe('open')
    expect(FakeWebSocket.rpcMethods).toContain('clarify.pending')
    expect(window.hermesDesktop?.getConnection).toHaveBeenCalledWith('research')
  })
})
