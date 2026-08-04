# VECTOR → Hermes v0.20.0 "Herald" upgrade runbook

Operational record + cutover procedure for upgrading the VECTOR profile
(`~/.hermes/profiles/vector`) to Hermes v0.20.0 (upstream "Herald" release,
2026-08-03) and running it as the persistent daily orchestrator.

**Prime directive (owner steer, 2026-08-04): do not deviate from the Hermes
Desktop.** The canonical Vector chat is the Desktop app's persistent session
(durable multi-client session work ported in the rebaseline). Everything in
this runbook protects that session; nothing replaces it.

## Version map

| Piece | Before | Target |
|---|---|---|
| `ace/patches` (fork prod branch) | 0.19.0 + fork patches | PR #21 rebaseline: upstream `f5be9236e` (0.20.0) + VECTOR overlay |
| Shared live install `~/.hermes/hermes-agent` | `ace/patches` @ 0.19.0 | fast-forward after #21 merges (nightly apply) |
| Vector launchd gateway (`ai.hermes.gateway-vector`) | shared install | shared install once 0.20, or profile runtime (see cutover) |
| Desktop-spawned vector backend | shared install (0.19, `dashboard --no-open` fallback) | shared install 0.20 (`serve`) — automatic after fast-forward |

## The one hazard that gates everything: state.db v25

0.19 (SCHEMA_VERSION 23) → 0.20 (25). Migration v25
(`hermes_state_schema.py::_dedupe_legacy_system_prompts`) moves every
`sessions.system_prompt` into a content-addressed `system_prompts` table and
sets `system_prompt = NULL` on the row. DDL is additive, but **a 0.19
process reading a v25 DB sees NULL system prompts for every pre-existing
session** (0.19 has no knowledge of the new table), and 0.19's on-open
reconcile/heal routines rewrite tables on every open.

Rules:
1. **Never let a 0.19 process open a home whose state.db a 0.20 process has
   migrated.** The Desktop currently spawns vector's backend from the shared
   0.19 install → the live vector home must NOT be opened by 0.20 until the
   shared install itself is 0.20.
2. Snapshot `state.db` (sqlite3 `.backup`) before the first 0.20 open of any
   home. `hermes update`'s own pre-update backup skips files > 1 GiB.
3. 0.20 reads 0.19 writes fine (old column kept as read fallback) — the
   upgrade direction is safe; only the mixed-version window is not.

## What was applied to the live profile (2026-08-04, pre-cutover safe set)

None of these open state.db with 0.20 code.

1. **Backup** → `profiles/vector/backups/pre-herald-20260804/`
   (state.db via sqlite3 `.backup` 309 MB, kanban.db, profile tar 52 MB,
   launchd plist copy).
2. **A3 config** via `scripts/vector/apply_vector_orchestrator_config.py`
   (ruamel round-trip, comments preserved, timestamped backup):
   - `platform_toolsets.{cli,telegram}` += browser, computer_use, cronjob,
     delegation, image_gen, kanban, tts, video, video_gen, vision, web
   - top-level `toolsets` += `kanban` (the kanban orchestrator gate reads
     this list, not platform_toolsets)
   - silence invariants: `voice.auto_tts: false`, `voice.beep_enabled: false`,
     `wake_word.enabled: false`, `discord.voice_fx.enabled: false`;
     `stt.enabled: true` (voice IN stays on, nothing speaks unattended).
     Audited `gateway_voice_mode.json`: absent → no per-chat override can
     speak either.
3. **A5 skills** via `scripts/vector/install_mp_skills.py`: mattpocock/skills
   v1.1.0 (stable release), manual-copy install (the hub GitHub installer
   silently drops sibling support files), 27 skills into
   `skills/mattpocock/mp-*` with frontmatter `name:` prefixed to match the
   dir (prefixing only the dir makes `skill_view` fail as ambiguous).
   Deduped: `code-review`/`grill-me` skipped (already visible unprefixed);
   `mp-tdd`/`mp-handoff`/`mp-grill-with-docs` yielded to the Ace.app vendored
   external dir (stale local copies removed). Post-condition: zero duplicate
   skill names across profile skills + both `skills.external_dirs`.
   Load proof: `hermes -p vector skills list` shows 35 enabled `mp-*` rows.
4. **Gateway restart** (launchd kickstart) to pick up config — same 0.19
   runtime, no schema risk. Telegram token lock keeps the reconnect
   profile-isolated (`acquire_scoped_lock` in the adapter).

## Staging validation of 0.20 (profile runtime, isolated home)

Runtime: `profiles/vector/runtime/hermes-agent` (clone of PR #21 head +
this branch), venv via
`UV_PROJECT_ENVIRONMENT=venv uv sync --extra messaging --extra edge-tts
--extra voice --extra dev --extra cli`, Python 3.11.14.
Staging home: `profiles/vector/runtime/staging-home` — migrated **copy** of
state.db, live config copy, skills copy, kanban dispatcher off, no telegram
token (prevents getUpdates conflicts with the live bot), heartbeat channel
inert.

Evidence captured:
- **Migration**: v25 migration of the 309 MB backup copy completed without
  exception; sessions/messages intact.
- **A1**: `hermes serve` (the Desktop's backend surface) boots in ~3 s;
  `session.list` shows the canonical Desktop session; `session.resume`
  restores its full transcript (93 messages) including compaction markers;
  two WS clients subscribe (owner + viewer) with fan-out; replay + ownership
  handoff exercised (see `e2e_client.py` in the staging home).
- **A2**: compaction archives visible in the resumed transcript; NEW
  behavioral tests `tests/test_compacted_transcript_visibility.py` (the
  overlay's `active = 1 OR compacted = 1` display projection previously had
  zero test coverage); heartbeat contract covered by
  `tests/gateway/test_heartbeat.py` (22 tests) and enabled in the live
  config; fresh-session rehydration = SessionEntry rehydrates from state.db
  alone (upstream `tests/gateway/test_session.py`).
- **A6 legs**: cron script-only job created + force-run → "Ran now:
  succeeded"; kanban CLI reads the shared root board on 0.20 (board writes
  deliberately not exercised from staging — the board is root-anchored and
  live); voice TTS→file→STT round-trip via edge-tts + faster-whisper, no
  audio played; delegation subagent turn via `prompt.submit`.
- **Tests** (0.20 tree + new venv, `scripts/run_tests.sh`):
  `tests/test_tui_gateway_server.py` + codex live-event files → 539 passed;
  A1/A2 focused files (session_stream, durable repro, queued-work,
  heartbeat) green; both fork E2Es
  (`tests/e2e/test_durable_multiclient_session.py`,
  `tests/e2e/test_update_drain_clarify.py`) green.

## Cutover procedure (run when PR #21 has merged)

1. Wait for the nightly apply to fast-forward `~/.hermes/hermes-agent` to
   the rebaselined `ace/patches` (or trigger it per its own runbook). This
   upgrades the Desktop backend and the default profile together.
2. `sqlite3 ~/.hermes/profiles/vector/state.db ".backup <backups>/state-pre-v25.db"`
3. Restart the vector gateway:
   `launchctl kickstart -k gui/$UID/ai.hermes.gateway-vector`
   (first 0.20 open migrates state.db to v25 — from this moment do not run
   0.19 binaries against the vector home).
4. Desktop: quit/relaunch (or let the pool respawn) — backend command
   auto-upgrades to `hermes serve`; the persistent session rehydrates from
   state.db.
5. Verify: `hermes -p vector --version` (0.20.0); gateway log shows telegram
   adapter reconnect + heartbeat on-start beat; Desktop restores the
   canonical chat with history.

Optional (pin the gateway to the profile runtime instead of the shared
install): render `scripts/vector/ai.hermes.gateway-vector.plist.template`
with `@RUNTIME@`/`@PROFILE_HOME@`, `launchctl bootout` + `bootstrap`. Run all
gateway lifecycle commands from that venv afterwards —
`refresh_launchd_plist_if_needed()` rewrites ProgramArguments from the
*running* interpreter. Keep macOS/Windows parity in mind: the launchd unit
is macOS; on Windows the equivalent is a Scheduled Task wrapping
`hermes --profile vector gateway run --replace` (upstream `hermes gateway
install` generates it).

## Rollback

- Config: restore `config.yaml.bak-orchestrator-<ts>` (applier's backup).
- Skills: remove `skills/mattpocock/` (curator-inert, no lock.json entries).
- Runtime: plist backup at `backups/pre-herald-20260804/`, `launchctl
  bootout` + `bootstrap` the old plist.
- Data: restore `backups/pre-herald-20260804/state.db` (pre-v25 snapshot)
  ONLY together with a 0.19 runtime; never mix versions against one home.

## Known follow-ups

- Venv SQLite is 3.50.4 (WAL-reset advisory) — hermes warns; upgrade the
  embedded runtime when upstream ships the bump.
- `apps/shared` `checkLiveness()`/`gateway.ping` client heartbeat is inert
  (no caller) — mobile-PWA resume after background-suspend has no coverage.
- The desktop composer still queues prompts client-side; server-side queue
  durability exists, but the client handoff remains a known gap.
