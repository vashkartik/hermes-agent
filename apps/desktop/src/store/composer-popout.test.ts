import { describe, expect, it } from 'vitest'

import { canUseComposerPopout, COMPOSER_POPOUT_MEDIA_QUERY } from './composer-popout'

describe('composer pop-out eligibility', () => {
  it('requires a desktop-like fine pointer viewport', () => {
    expect(canUseComposerPopout(false, true)).toBe(true)
    expect(canUseComposerPopout(false, false)).toBe(false)
  })

  it('keeps secondary windows docked even on desktop viewports', () => {
    expect(canUseComposerPopout(true, true)).toBe(false)
  })

  it('uses a media query that excludes narrow and coarse-pointer layouts', () => {
    expect(COMPOSER_POPOUT_MEDIA_QUERY).toContain('hover: hover')
    expect(COMPOSER_POPOUT_MEDIA_QUERY).toContain('pointer: fine')
    expect(COMPOSER_POPOUT_MEDIA_QUERY).toContain('min-width: 48rem')
  })
})
