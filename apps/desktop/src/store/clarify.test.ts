import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $clarifyRequest,
  $clarifyRequests,
  type ClarifyRequest,
  clearClarifyRequest,
  normalizeChoices,
  setClarifyRequest,
  syncPendingClarifyRequests
} from './clarify'
import { $activeSessionId } from './session'

function clarify(sessionId: string | null, requestId: string): ClarifyRequest {
  return {
    requestId,
    question: `question-${requestId}`,
    choices: null,
    sessionId
  }
}

describe('clarify store', () => {
  beforeEach(() => {
    $clarifyRequests.set({})
    $activeSessionId.set(null)
  })

  afterEach(() => {
    $clarifyRequests.set({})
    $activeSessionId.set(null)
  })

  it('keeps clarify requests from concurrent sessions independent', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    expect($clarifyRequests.get()['session-a']?.requestId).toBe('req-a')
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('req-b')
  })

  it('exposes only the active session via the focus-scoped view', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    $activeSessionId.set('session-a')
    expect($clarifyRequest.get()?.requestId).toBe('req-a')

    $activeSessionId.set('session-b')
    expect($clarifyRequest.get()?.requestId).toBe('req-b')

    $activeSessionId.set('session-c')
    expect($clarifyRequest.get()).toBeNull()
  })

  it('clears only the targeted session, leaving the other pending', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    clearClarifyRequest('req-a', 'session-a')

    expect($clarifyRequests.get()['session-a']).toBeUndefined()
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('req-b')
  })

  it('ignores a stale clear whose request id no longer matches', () => {
    setClarifyRequest(clarify('session-a', 'req-a2'))

    clearClarifyRequest('req-a1', 'session-a')

    expect($clarifyRequests.get()['session-a']?.requestId).toBe('req-a2')
  })

  it('clears by request id across sessions when no session hint is given', () => {
    setClarifyRequest(clarify('session-a', 'shared'))
    setClarifyRequest(clarify('session-b', 'other'))

    clearClarifyRequest('shared')

    expect($clarifyRequests.get()['session-a']).toBeUndefined()
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('other')
  })

  it('restores pending clarify requests for every backend session', async () => {
    const request = vi.fn().mockResolvedValue({
      requests: [
        {
          request_id: 'req-a',
          session_id: 'session-a',
          question: 'Question A',
          choices: ['One', 'Two']
        },
        {
          request_id: 'req-b',
          session_id: 'session-b',
          question: 'Question B',
          choices: null
        }
      ]
    })

    await syncPendingClarifyRequests({ request } as never)

    expect(request).toHaveBeenCalledWith('clarify.pending', {})
    expect($clarifyRequests.get()).toEqual({
      'session-a': {
        requestId: 'req-a',
        sessionId: 'session-a',
        question: 'Question A',
        choices: ['One', 'Two']
      },
      'session-b': {
        requestId: 'req-b',
        sessionId: 'session-b',
        question: 'Question B',
        choices: null
      }
    })
  })

  it('ignores malformed or empty pending rows', async () => {
    const request = vi.fn().mockResolvedValue({
      requests: [
        {
          request_id: 'valid',
          session_id: 'session-a',
          question: 'Still valid',
          choices: ['Yes']
        },
        { request_id: '', session_id: 'session-b', question: 'Missing id' },
        { request_id: 'missing-question', session_id: 'session-c', question: '' },
        { request_id: 'bad-session', session_id: 42, question: 'Wrong session' },
        {
          request_id: 'bad-choices',
          session_id: 'session-d',
          question: 'Wrong choices',
          choices: ['Yes', 42]
        },
        null
      ]
    })

    await syncPendingClarifyRequests({ request } as never)

    expect($clarifyRequests.get()).toEqual({
      'session-a': {
        requestId: 'valid',
        sessionId: 'session-a',
        question: 'Still valid',
        choices: ['Yes']
      }
    })
  })

  it('replaying the same pending request preserves store identity', async () => {
    const request = vi.fn().mockResolvedValue({
      requests: [
        {
          request_id: 'req-a',
          session_id: 'session-a',
          question: 'Question A',
          choices: ['One', 'Two']
        }
      ]
    })

    await syncPendingClarifyRequests({ request } as never)
    const first = $clarifyRequests.get()
    await syncPendingClarifyRequests({ request } as never)

    expect($clarifyRequests.get()).toBe(first)
  })

  it('leaves existing live requests intact when replay is unavailable', async () => {
    setClarifyRequest(clarify('session-live', 'req-live'))
    const before = $clarifyRequests.get()
    const request = vi.fn().mockRejectedValue(new Error('unknown method: clarify.pending'))

    await expect(syncPendingClarifyRequests({ request } as never)).rejects.toThrow(
      'unknown method: clarify.pending'
    )

    expect($clarifyRequests.get()).toBe(before)
  })
})

describe('normalizeChoices', () => {
  it('returns empty array for null/undefined', () => {
    expect(normalizeChoices(null)).toEqual([])
    expect(normalizeChoices(undefined)).toEqual([])
  })

  it('returns empty array for non-array input', () => {
    expect(normalizeChoices('hello')).toEqual([])
    expect(normalizeChoices(42)).toEqual([])
    expect(normalizeChoices({})).toEqual([])
  })

  it('filters out non-string items', () => {
    expect(normalizeChoices(['a', 42, 'b', null, 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('drops blank and whitespace-only strings', () => {
    expect(normalizeChoices(['a', '', 'b', '   ', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('drops strings with newlines', () => {
    expect(normalizeChoices(['a', 'b\nc', 'd'])).toEqual(['a', 'd'])
  })

  it('drops strings over 200 chars', () => {
    const long = 'x'.repeat(201)
    const ok = 'y'.repeat(200)
    expect(normalizeChoices(['a', long, ok])).toEqual(['a', ok])
  })

  it('drops empty items and keeps valid ones', () => {
    expect(normalizeChoices(['valid', '  ', '', 'also valid'])).toEqual(['valid', 'also valid'])
  })

  it('returns empty array when nothing survives', () => {
    expect(normalizeChoices(['', '  ', null, undefined])).toEqual([])
    expect(normalizeChoices([])).toEqual([])
  })
})
