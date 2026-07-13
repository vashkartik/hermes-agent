import { afterEach, describe, expect, it } from 'vitest'

import { $activeSessionId, setActiveSessionId } from './active-session'
import { $clarifyRequest, clearClarifyRequest, setClarifyRequest } from './clarify'
import {
  $activeSessionId as $activeSessionIdFromSession,
  setActiveSessionId as setActiveSessionIdFromSession
} from './session'

describe('active session store boundary', () => {
  afterEach(() => {
    clearClarifyRequest()
    setActiveSessionId(null)
  })

  it('keeps session consumers and cycle-sensitive prompt selectors on one leaf atom', () => {
    expect($activeSessionIdFromSession).toBe($activeSessionId)
    expect(setActiveSessionIdFromSession).toBe(setActiveSessionId)

    setClarifyRequest({
      choices: null,
      question: 'Still there?',
      requestId: 'request-1',
      sessionId: 'session-1'
    })

    const observed: Array<string | null> = []
    const unbind = $clarifyRequest.subscribe(request => observed.push(request?.requestId ?? null))

    setActiveSessionId('session-1')
    setActiveSessionId(null)
    unbind()

    expect(observed).toEqual([null, 'request-1', null])
  })
})
