import { useStore } from '@nanostores/react'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { QueueEditState } from '@/app/chat/composer/composer-utils'
import { useComposerQueue } from '@/app/chat/composer/hooks/use-composer-queue'
import type { ChatBarProps } from '@/app/chat/composer/types'
import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $compactingSessions, isSessionCompacting, setSessionCompacting } from '@/store/compaction'
import { $parkedQueueSessions, $queuedPromptsBySession, enqueueQueuedPrompt } from '@/store/composer-queue'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-1'
const OTHER_SID = 'session-2'
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

function emit(type: RpcEvent['type'], payload: RpcEvent['payload'] = {}) {
  act(() => handleEvent!({ payload, session_id: SID, type }))
}

function QueueHarness({ onSubmit }: { onSubmit: ChatBarProps['onSubmit'] }) {
  const compactingSessions = useStore($compactingSessions)
  const queueEditRef = useRef<QueueEditState | null>(null)

  useComposerQueue({
    activeQueueSessionKey: SID,
    attachments: [],
    busy: isSessionCompacting(SID, compactingSessions),
    clearDraft: () => undefined,
    draftRef: { current: '' },
    focusInput: () => undefined,
    loadIntoComposer: () => undefined,
    onCancel: () => undefined,
    onSubmit,
    queueEditRef,
    queueSessionKey: SID,
    sessionId: SID
  })

  return null
}

describe('useMessageStream compaction lifecycle', () => {
  beforeEach(() => {
    handleEvent = null
    window.localStorage.clear()
    $compactingSessions.set({})
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
  })

  afterEach(() => {
    cleanup()
    $compactingSessions.set({})
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    vi.restoreAllMocks()
  })

  it.each([
    ['message.delta', { text: 'resumed' }],
    ['thinking.delta', { text: 'still working' }],
    ['reasoning.delta', { text: 'thinking again' }],
    ['tool.start', { name: 'terminal', tool_id: 'tool-1' }]
  ] as const)('clears the stale compaction phase when %s resumes the turn', async (type, payload) => {
    await mountStream()
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compacting' })
    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true, [SID]: true })

    emit(type, payload)

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })
  })

  it('keeps the compaction phase through compacted until the original turn visibly resumes', async () => {
    await mountStream()
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compacting' })
    emit('status.update', { kind: 'compacted' })

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true, [SID]: true })

    emit('message.delta', { text: 'original turn resumed' })

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })
  })

  it('clears the compaction latch on a terminal completion without a resume delta', async () => {
    await mountStream()

    emit('status.update', { kind: 'compacting' })
    emit('status.update', { kind: 'compacted' })
    emit('message.complete', { text: 'turn finished at the compaction boundary' })

    expect($compactingSessions.get()).toEqual({})

    // The terminal also retires the internal resume sentinel. A later delta
    // must not clear a new, independently armed compaction flag.
    setSessionCompacting(SID, true)
    emit('message.delta', { text: 'later turn' })
    expect($compactingSessions.get()).toEqual({ [SID]: true })
  })

  it('retires only the addressed compaction on the manual-compress ready terminal', async () => {
    await mountStream()
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compacting' })
    emit('status.update', { kind: 'compacted' })
    emit('status.update', { kind: 'ready' })

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })

    // `ready` must also retire the internal resume sentinel. A later delta
    // cannot clear a newly armed, independent phase for the same session.
    setSessionCompacting(SID, true)
    emit('message.delta', { text: 'later independent turn' })
    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true, [SID]: true })
  })

  it('does not make a queued follow-up eligible until the original turn resumes after compacted', async () => {
    await mountStream()
    emit('status.update', { kind: 'compacting' })
    enqueueQueuedPrompt(SID, { attachments: [], text: 'queued follow-up' })

    const onSubmit = vi.fn<ChatBarProps['onSubmit']>(async () => true)
    render(<QueueHarness onSubmit={onSubmit} />)

    await act(async () => Promise.resolve())
    expect(onSubmit).not.toHaveBeenCalled()

    emit('status.update', { kind: 'compacted' })
    await act(async () => Promise.resolve())
    expect(onSubmit).not.toHaveBeenCalled()

    emit('message.delta', { text: 'original continuation first' })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit).toHaveBeenCalledWith('queued follow-up', {
      attachments: [],
      fromQueue: true,
      sessionId: SID,
      storedSessionId: SID
    })
  })
})
