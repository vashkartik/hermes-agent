type MicrophoneStatus = 'granted' | 'denied' | 'restricted' | 'not-determined' | 'unknown' | string

export interface MicrophoneSystemPreferences {
  getMediaAccessStatus?: (mediaType: 'microphone') => MicrophoneStatus
  askForMediaAccess?: (mediaType: 'microphone') => Promise<boolean>
}

/**
 * Build the desktop microphone permission gate once per Electron main process.
 * Existing grants never re-enter TCC's request path, denied/restricted access
 * stays quiet, and concurrent first-use controls share one OS request.
 */
export function createMicrophoneAccessRequester(
  isMac: boolean,
  preferences: MicrophoneSystemPreferences
): () => Promise<boolean> {
  let pending: Promise<boolean> | null = null

  return async () => {
    if (!isMac) {
      return true
    }

    let status: MicrophoneStatus = 'unknown'

    try {
      status = preferences.getMediaAccessStatus?.('microphone') ?? 'unknown'
    } catch {
      status = 'unknown'
    }

    if (status === 'granted') {
      return true
    }

    if (status === 'denied' || status === 'restricted') {
      return false
    }

    if (typeof preferences.askForMediaAccess !== 'function') {
      return false
    }

    if (!pending) {
      pending = Promise.resolve()
        .then(() => preferences.askForMediaAccess?.('microphone'))
        .then(Boolean)
        .finally(() => {
          pending = null
        })
    }

    return pending
  }
}
