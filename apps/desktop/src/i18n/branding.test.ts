import { afterEach, describe, expect, it } from 'vitest'

import {
  __resetAgentBranding,
  applyAgentBranding,
  brandCatalogInPlace,
  brandString
} from './branding'
import { TRANSLATIONS } from './catalog'

describe('brandString', () => {
  it('replaces the bare product name and its compound forms', () => {
    expect(brandString('Ask Hermes…', 'VECTOR')).toBe('Ask VECTOR…')
    expect(brandString('Starting Hermes Desktop…', 'VECTOR')).toBe('Starting VECTOR…')
    expect(brandString("Hermes Agent couldn't start", 'VECTOR')).toBe("VECTOR couldn't start")
  })

  it('leaves identifiers and URLs that merely contain the name intact', () => {
    // Word-boundary match: lowercase/technical tokens are not display copy.
    expect(brandString('hermes_cli/web_dist', 'VECTOR')).toBe('hermes_cli/web_dist')
    expect(brandString('https://hermes.example', 'VECTOR')).toBe('https://hermes.example')
  })
})

describe('brandCatalogInPlace', () => {
  it('rewrites nested objects and arrays of strings in place', () => {
    const fixture = {
      boot: { ready: 'Hermes Desktop is ready', steps: ['Starting Hermes Desktop…', 'done'] },
      other: 42
    }

    brandCatalogInPlace(fixture, 'VECTOR')

    expect(fixture.boot.ready).toBe('VECTOR is ready')
    expect(fixture.boot.steps[0]).toBe('Starting VECTOR…')
    expect(fixture.other).toBe(42)
  })
})

describe('applyAgentBranding', () => {
  afterEach(() => __resetAgentBranding())

  it('ignores empty and default names', () => {
    expect(applyAgentBranding('')).toBe(false)
    expect(applyAgentBranding('   ')).toBe(false)
    expect(applyAgentBranding('Hermes')).toBe(false)
    expect(applyAgentBranding('Hermes Agent')).toBe(false)
    expect(applyAgentBranding('hermes desktop')).toBe(false)
  })

  it('rebrands the live catalog once and only once', () => {
    expect(applyAgentBranding('VECTOR')).toBe(true)
    expect(TRANSLATIONS.en.boot.ready).toContain('VECTOR')
    expect(JSON.stringify(TRANSLATIONS.en)).not.toMatch(/\bHermes(?: Desktop| Agent)?\b/)
    // Second application (same or different name) is a no-op.
    expect(applyAgentBranding('VECTOR')).toBe(false)
    expect(applyAgentBranding('Other')).toBe(false)
  })
})
