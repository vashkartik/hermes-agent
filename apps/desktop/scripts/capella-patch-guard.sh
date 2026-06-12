#!/usr/bin/env bash
# Capella fork patch guard.
#
# Run after rebasing `capella/patches` onto upstream to confirm our patches
# SURVIVED the rebase (their markers are still present) and the desktop app
# still builds. Exits non-zero on any missing patch so a clobbering upstream
# rebase fails loudly instead of silently dropping our changes.
#
#   bash apps/desktop/scripts/capella-patch-guard.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESK="$ROOT/apps/desktop"
fail=0

check() { # <file> <marker> <description>
  if grep -q "$2" "$1" 2>/dev/null; then
    echo "  ok   $3"
  else
    echo "  MISS $3  ($1 :: /$2/)"
    fail=1
  fi
}

echo "Capella patch guard — verifying our fork patches survived:"
check "$DESK/src/components/chat/intro.tsx"      "HERO_EMOJI_BY_KEY"  "hero: per-agent emoji + name (King/Rook)"
check "$DESK/src/store/session.ts"               "hermes:last-session:" "persistence: last session per profile"
check "$DESK/src/store/session.ts"               'selectedStoredSessionId.subscribe' "persistence: last-session stores STORED ids (not runtime ids)"
check "$DESK/src/app/desktop-controller.tsx"     "lastSessionFor"     "persistence: restore chat on profile switch"
# Upstream absorbed our needsReattach patch as an inline gate (with their own
# stuckOnRoutedSession improvement); guard the behavior, not our old variable.
check "$DESK/src/app/session/hooks/use-route-resume.ts" "gatewayBecameOpen || !alreadyActive" "gateway client: re-attach session stream on every fresh socket"
check "$ROOT/tui_gateway/server.py"              "_rebind_ws_transport" "gateway: stdio black-hole re-bind on session-scoped RPC"
check "$ROOT/tui_gateway/server.py"              "_persist_session_history" "gateway: turn-end transcript persistence"
check "$ROOT/agent/codex_runtime.py"             "run_conversation already appended the user message" "codex runtime: no duplicate user echo in spliced transcript"
check "$ROOT/scripts/install.sh"                 "HERMES_REPO_URL"    "install: pinned-fork repo override for embedders"
check "$ROOT/hermes_cli/web_server.py"           "api/memory/facts"   "memory: read-only facts browser endpoint (Memories page REST)"
check "$ROOT/hermes_cli/web_server.py"           "_sessions_daily_stats" "sessions: per-day stats breakdown in /api/sessions/stats"
check "$DESK/src/app/memories/index.tsx"         "MemoriesView"       "memories: animated facts browser page (motion)"
check "$DESK/src/app/routes.ts"                  "MEMORIES_ROUTE"     "memories: /memories route + sidebar nav entry"
[ -f "$DESK/CAPELLA_FORK.md" ] && echo "  ok   fork doc (CAPELLA_FORK.md)" || { echo "  MISS CAPELLA_FORK.md"; fail=1; }

# Gateway patch — best-effort: only checks when upstream is fetched locally.
if git -C "$ROOT" rev-parse --verify -q upstream/main >/dev/null 2>&1; then
  if git -C "$ROOT" diff --quiet upstream/main -- hermes_cli/gateway.py 2>/dev/null; then
    echo "  MISS gateway: hermes_cli/gateway.py matches upstream (patch lost?)"
    fail=1
  else
    echo "  ok   gateway: hermes_cli/gateway.py patch present"
  fi
else
  echo "  --   gateway: skipped (fetch 'upstream' to verify the gateway.py patch)"
fi

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "PATCH GUARD FAILED — a Capella patch is missing. A rebase likely dropped it."
  echo "Re-apply from the capella/patches history before rebuilding/shipping."
  exit 1
fi
echo "patch guard passed — all Capella patches present."
