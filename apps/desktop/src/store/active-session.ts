import { atom } from 'nanostores'

type ActiveSessionUpdater = string | null | ((current: string | null) => string | null)

// Cycle-free leaf store for the runtime session currently shown in this
// window. Prompt selectors are imported by the composer during renderer boot;
// keeping their session dependency outside the broad session module prevents
// the production bundle from capturing an uninitialised circular import.
export const $activeSessionId = atom<string | null>(null)

export const setActiveSessionId = (next: ActiveSessionUpdater): void => {
  $activeSessionId.set(typeof next === 'function' ? next($activeSessionId.get()) : next)
}
