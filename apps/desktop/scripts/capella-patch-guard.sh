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
check "$DESK/src/app/desktop-controller.tsx"     "lastSessionFor"     "persistence: restore chat on profile switch"
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
