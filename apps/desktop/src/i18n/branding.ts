/**
 * Agent-name branding from the backend skin.
 *
 * Hermes skins already carry `branding.agent_name` (the TUI renders it in its
 * banner and status line), and every desktop client receives the resolved skin
 * on `gateway.ready` / `skin.changed`. The desktop's visible copy, however, is
 * a static i18n catalog full of literal "Hermes" / "Hermes Desktop" strings —
 * so a user who named their agent saw the name nowhere in this UI.
 *
 * Rather than threading a name through hundreds of call sites, the catalog is
 * rebranded once, in place, when a skin announces a non-default agent name.
 * One choke point, every locale, every screen — boot overlay, settings copy,
 * quick-entry placeholder, all of it.
 *
 * One-way by design: after the first application the strings no longer
 * contain "Hermes", so a later rename needs an app reload (skins change
 * rarely; a reload is how the desktop picks up most backend identity changes
 * anyway).
 */

import { TRANSLATIONS } from './catalog'

const HERMES_NAME_RE = /\bHermes(?: Desktop| Agent)?\b/g

let appliedName: string | null = null

/** Replace visible Hermes product-name mentions in one string. */
export function brandString(value: string, agentName: string): string {
  return value.replace(HERMES_NAME_RE, agentName)
}

/** Deep in-place rebrand of a translations tree (objects/arrays of strings). */
export function brandCatalogInPlace(node: unknown, agentName: string): void {
  if (Array.isArray(node)) {
    for (let i = 0; i < node.length; i += 1) {
      const item = node[i]

      if (typeof item === 'string') {
        node[i] = brandString(item, agentName)
      } else {
        brandCatalogInPlace(item, agentName)
      }
    }

    return
  }

  if (typeof node !== 'object' || node === null) {
    return
  }

  const record = node as Record<string, unknown>

  for (const key of Object.keys(record)) {
    const value = record[key]

    if (typeof value === 'string') {
      record[key] = brandString(value, agentName)
    } else {
      brandCatalogInPlace(value, agentName)
    }
  }
}

/**
 * Apply the skin's agent name to the whole UI. Returns true when a rebrand
 * actually happened. No-ops for empty/default names and repeat calls.
 */
export function applyAgentBranding(rawName: unknown): boolean {
  const agentName = String(rawName ?? '').trim()

  if (!agentName || /^hermes(?: agent| desktop)?$/i.test(agentName)) {
    return false
  }

  if (appliedName !== null) {
    // Catalog strings no longer contain "Hermes" — a second pass would no-op
    // for the same name and cannot rename for a different one. See module doc.
    return appliedName === agentName ? false : false
  }

  appliedName = agentName

  for (const locale of Object.keys(TRANSLATIONS) as Array<keyof typeof TRANSLATIONS>) {
    brandCatalogInPlace(TRANSLATIONS[locale], agentName)
  }

  try {
    if (typeof document !== 'undefined' && document.title) {
      document.title = brandString(document.title, agentName)
    }
  } catch {
    // Title is cosmetic; never let it break skin ingestion.
  }

  return true
}

/** Test-only: clear the one-shot guard (catalog mutations are not restored). */
export function __resetAgentBranding(): void {
  appliedName = null
}
