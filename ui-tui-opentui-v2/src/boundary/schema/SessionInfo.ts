/**
 * SessionInfo + Catalog decoders — the decode-at-boundary idiom (spec v4 §3.3),
 * mirroring GatewayEvent.ts. These two payloads are UNTRUSTED loose JSON from the
 * Python `tui_gateway` (`session.info` event / `session.create`/`resume` result
 * `info`, and the `startup.catalog` RPC result), so they are decoded ONCE with an
 * Effect Schema instead of hand-rolled `as`-cast readers.
 *
 * Decode with `Schema.decodeUnknownOption`: a malformed/partial payload yields
 * `Option.none` and the caller falls back to an empty patch / leaves the catalog
 * unset — a stray shape never crashes the reducer.
 *
 * Wire field names are verified against `tui_gateway/server.py`:
 *   - session.info  → `_session_info()` (server.py:~1830): top-level `model`,
 *     `reasoning_effort`, `fast`, `cwd`, `branch`, `running`, plus a nested
 *     `usage` (`_get_usage()`, server.py:~1698) carrying `context_used`,
 *     `context_max`, `context_percent`, `compressions` (context_* only present
 *     when the compressor knows a context length).
 *   - startup.catalog → `@method("startup.catalog")` (server.py:~8521):
 *     `{ tools:{total, toolsets:[{name,count,enabled,tools}]},
 *        skills:{total, categories:[{name,count}]}, mcp:{servers:[]} }`.
 *
 * These schemas are used PURELY as decoders; they do NOT Effect-ify the store's
 * reactivity or control flow (Solid stays the runtime — spec v4 §1).
 */
import { Schema } from 'effect'

const Str = Schema.String
const Num = Schema.Number
const Bool = Schema.Boolean
const opt = Schema.optionalKey

// ── session.info / session.create.info ────────────────────────────────
// Context/usage numbers arrive nested under `usage`; the same names may also
// appear at the top level depending on the RPC vs event path (the reader prefers
// `usage.context_*`, then the top-level fallback). All keys are optional — a
// `session.info` patch only carries the fields that actually changed.
const UsageSchema = Schema.Struct({
  context_used: opt(Num),
  context_max: opt(Num),
  context_percent: opt(Num),
  compressions: opt(Num)
})

export const SessionInfoPatchSchema = Schema.Struct({
  model: opt(Str),
  reasoning_effort: opt(Str),
  fast: opt(Bool),
  cwd: opt(Str),
  branch: opt(Str),
  running: opt(Bool),
  // top-level context fallback (used when there's no nested `usage`)
  context_used: opt(Num),
  context_max: opt(Num),
  context_percent: opt(Num),
  compressions: opt(Num),
  usage: opt(UsageSchema)
})
export type SessionInfoPatchDecoded = typeof SessionInfoPatchSchema.Type

/** Decode a loose session.info payload → `Option<SessionInfoPatchDecoded>`. */
export const decodeSessionInfoPatch = Schema.decodeUnknownOption(SessionInfoPatchSchema)

// ── startup.catalog ───────────────────────────────────────────────────
// Mirrors the `Catalog` interface in store.ts. `enabled` defaults to true at the
// reader (an absent flag means on), so it stays optional here.
const ToolsetSchema = Schema.Struct({
  name: opt(Str),
  count: opt(Num),
  enabled: opt(Bool),
  tools: opt(Schema.Array(Schema.Unknown))
})
const CategorySchema = Schema.Struct({
  name: opt(Str),
  count: opt(Num)
})

export const CatalogSchema = Schema.Struct({
  tools: opt(
    Schema.Struct({
      total: opt(Num),
      toolsets: opt(Schema.Array(ToolsetSchema))
    })
  ),
  skills: opt(
    Schema.Struct({
      total: opt(Num),
      categories: opt(Schema.Array(CategorySchema))
    })
  ),
  mcp: opt(
    Schema.Struct({
      servers: opt(Schema.Array(Schema.Unknown))
    })
  )
})
export type CatalogDecoded = typeof CatalogSchema.Type

/** Decode a loose startup.catalog result → `Option<CatalogDecoded>`. */
export const decodeCatalog = Schema.decodeUnknownOption(CatalogSchema)
