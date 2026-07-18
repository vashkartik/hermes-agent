import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $queuedPromptsBySession,
  clearQueuedPrompts,
  enqueueQueuedPrompt,
  getQueuedPrompts
} from '@/store/composer-queue'

import { useComposerQueue } from './use-composer-queue'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: { composer: { queueStuckBody: 'stuck', queueStuckTitle: 'Queue stuck' } }
  })
}))
vi.mock('@/lib/haptics', () => ({ triggerHaptic: () => undefined }))
vi.mock('@/store/notifications', () => ({ notify: () => undefined }))

const SESSION_ID = 'session-queue-race'

describe('useComposerQueue send now', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $queuedPromptsBySession.set({})
  })

  afterEach(() => {
    clearQueuedPrompts(SESSION_ID)
    vi.restoreAllMocks()
  })

  it('submits a busy queued prompt atomically without a separate interrupt', async () => {
    const entry = enqueueQueuedPrompt(SESSION_ID, { attachments: [], text: 'check the new logs' })!
    const onCancel = vi.fn(async () => undefined)
    const onSubmit = vi.fn(async () => true)

    const args = {
      activeQueueSessionKey: SESSION_ID,
      attachments: [],
      busy: true,
      clearDraft: vi.fn(),
      draftRef: { current: '' },
      focusInput: vi.fn(),
      loadIntoComposer: vi.fn(),
      onCancel,
      onSubmit,
      queueEditRef: { current: null },
      queueSessionKey: SESSION_ID,
      sessionId: SESSION_ID
    }

    const { result } = renderHook(() => useComposerQueue(args))

    await act(async () => {
      await result.current.sendQueuedNow(entry.id)
    })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    // Tile-scoped composers pin sessionId/storedSessionId on queued submits;
    // this test only cares that the queued text goes out atomically.
    expect(onSubmit).toHaveBeenCalledWith(
      'check the new logs',
      expect.objectContaining({
        attachments: [],
        fromQueue: true
      })
    )
    expect(onCancel).not.toHaveBeenCalled()
    expect(getQueuedPrompts(SESSION_ID)).toEqual([])
  })
})
