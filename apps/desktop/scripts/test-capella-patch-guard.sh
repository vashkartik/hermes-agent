#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
GUARD="$ROOT/apps/desktop/scripts/capella-patch-guard.sh"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

copy_sources() {
  mkdir -p \
    "$TMP_ROOT/tui_gateway" \
    "$TMP_ROOT/apps/desktop/src/store" \
    "$TMP_ROOT/apps/desktop/src/app/gateway/hooks"
  cp "$ROOT/tui_gateway/server.py" "$TMP_ROOT/tui_gateway/server.py"
  cp "$ROOT/apps/desktop/src/store/active-session.ts" "$TMP_ROOT/apps/desktop/src/store/active-session.ts"
  cp "$ROOT/apps/desktop/src/store/clarify.ts" "$TMP_ROOT/apps/desktop/src/store/clarify.ts"
  cp "$ROOT/apps/desktop/src/store/gateway.ts" "$TMP_ROOT/apps/desktop/src/store/gateway.ts"
  cp \
    "$ROOT/apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts" \
    "$TMP_ROOT/apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts"
}

run_guard() {
  CAPELLA_PATCH_GUARD_ROOT="$TMP_ROOT" \
    CAPELLA_PATCH_GUARD_CLARIFY_ONLY=1 \
    bash "$GUARD"
}

assert_missing_marker_fails() {
  local relative_file="$1"
  local marker="$2"
  local description="$3"
  local output
  local status

  copy_sources
  grep -vF "$marker" "$TMP_ROOT/$relative_file" > "$TMP_ROOT/without-marker"
  mv "$TMP_ROOT/without-marker" "$TMP_ROOT/$relative_file"

  set +e
  output="$(run_guard 2>&1)"
  status=$?
  set -e

  if [ "$status" -eq 0 ] || ! grep -Fq "MISS $description" <<< "$output"; then
    echo "expected guard failure for: $description" >&2
    echo "$output" >&2
    exit 1
  fi
}

copy_sources
run_guard >/dev/null

assert_missing_marker_fails \
  "tui_gateway/server.py" \
  "timeout=None" \
  "clarify: desktop wait has no wall-clock timeout"
assert_missing_marker_fails \
  "tui_gateway/server.py" \
  '@method("clarify.pending")' \
  "clarify: backend exposes live pending requests"
assert_missing_marker_fails \
  "apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts" \
  "syncPendingClarifyRequests(gateway)" \
  "clarify: primary sockets replay pending requests"
assert_missing_marker_fails \
  "apps/desktop/src/store/gateway.ts" \
  "syncPendingClarifyRequests(entry.gateway)" \
  "clarify: secondary profile sockets replay pending requests"
assert_missing_marker_fails \
  "apps/desktop/src/store/clarify.ts" \
  "from './active-session'" \
  "clarify: selector uses cycle-free active-session leaf"

echo "ok - patch guard protects persistent clarify recovery"
