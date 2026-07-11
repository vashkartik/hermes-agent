import assert from 'node:assert/strict'
import test from 'node:test'

import { createMicrophoneAccessRequester } from './microphone-access'

test('non-macOS platforms do not request macOS media access', async () => {
  let asks = 0

  const request = createMicrophoneAccessRequester(false, {
    askForMediaAccess: async () => {
      asks += 1

      return true
    }
  })

  assert.equal(await request(), true)
  assert.equal(asks, 0)
})

test('an existing grant returns without requesting again', async () => {
  let asks = 0

  const request = createMicrophoneAccessRequester(true, {
    getMediaAccessStatus: () => 'granted',
    askForMediaAccess: async () => {
      asks += 1

      return true
    }
  })

  assert.equal(await request(), true)
  assert.equal(asks, 0)
})

test('denied and restricted statuses do not re-prompt', async () => {
  let status = 'denied'
  let asks = 0

  const request = createMicrophoneAccessRequester(true, {
    getMediaAccessStatus: () => status,
    askForMediaAccess: async () => {
      asks += 1

      return true
    }
  })

  assert.equal(await request(), false)
  status = 'restricted'
  assert.equal(await request(), false)
  assert.equal(asks, 0)
})

test('concurrent first-use requests share one OS prompt', async () => {
  let asks = 0

  let resolveAccess: (allowed: boolean) => void = () => {}

  const request = createMicrophoneAccessRequester(true, {
    getMediaAccessStatus: () => 'not-determined',
    askForMediaAccess: () => {
      asks += 1

      return new Promise(resolve => {
        resolveAccess = resolve
      })
    }
  })

  const first = request()
  const second = request()
  await Promise.resolve()
  assert.equal(asks, 1)
  resolveAccess(true)
  assert.deepEqual(await Promise.all([first, second]), [true, true])
})
