import { expect, test } from 'vitest'

import { createMicrophoneAccessRequester } from './microphone-access'

test('non-macOS platforms do not request macOS media access', async () => {
  let asks = 0

  const request = createMicrophoneAccessRequester(false, {
    askForMediaAccess: async () => {
      asks += 1

      return true
    }
  })

  expect(await request()).toBe(true)
  expect(asks).toBe(0)
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

  expect(await request()).toBe(true)
  expect(asks).toBe(0)
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

  expect(await request()).toBe(false)
  status = 'restricted'
  expect(await request()).toBe(false)
  expect(asks).toBe(0)
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
  expect(asks).toBe(1)
  resolveAccess(true)
  expect(await Promise.all([first, second])).toEqual([true, true])
})
