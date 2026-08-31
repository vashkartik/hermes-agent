#!/usr/bin/env bash
# Provision (or upgrade) a profile-scoped Hermes runtime for the VECTOR
# profile, without touching the shared ~/.hermes/hermes-agent install that
# other profiles and the nightly-apply pipeline own.
#
# Layout produced:
#   $PROFILE_HOME/runtime/hermes-agent        # git checkout at $REF
#   $PROFILE_HOME/runtime/hermes-agent/venv   # uv-managed venv (python 3.11)
#
# Usage:
#   provision-vector-runtime.sh [REF] [SOURCE_REPO]
#     REF          git ref to check out (default: vector-daily-hermes-feat-bug)
#     SOURCE_REPO  clone source (default: https://github.com/vashkartik/hermes-agent.git)
#
# Idempotent: re-running fetches + hard-resets the runtime checkout to REF
# and re-syncs the venv. Run a profile backup first (see the runbook).
set -euo pipefail

PROFILE_HOME="${PROFILE_HOME:-$HOME/.hermes/profiles/vector}"
REF="${1:-vector-daily-hermes-feat-bug}"
SOURCE_REPO="${2:-https://github.com/vashkartik/hermes-agent.git}"
RUNTIME_DIR="$PROFILE_HOME/runtime/hermes-agent"
EXTRAS=(--extra messaging --extra edge-tts --extra voice --extra dev --extra cli)

command -v uv >/dev/null || { echo "uv is required (brew install uv)" >&2; exit 1; }
[ -d "$PROFILE_HOME" ] || { echo "profile home not found: $PROFILE_HOME" >&2; exit 1; }

if [ ! -d "$RUNTIME_DIR/.git" ]; then
    mkdir -p "$(dirname "$RUNTIME_DIR")"
    git clone "$SOURCE_REPO" "$RUNTIME_DIR"
fi
git -C "$RUNTIME_DIR" fetch origin "$REF"
git -C "$RUNTIME_DIR" checkout -q FETCH_HEAD 2>/dev/null || git -C "$RUNTIME_DIR" checkout -q "$REF"
echo "runtime at: $(git -C "$RUNTIME_DIR" log --oneline -1)"

cd "$RUNTIME_DIR"
UV_PROJECT_ENVIRONMENT=venv uv sync "${EXTRAS[@]}"
venv/bin/python -c "import hermes_constants, sys; print('runtime import ok:', sys.version.split()[0])"
venv/bin/python -c "import importlib.metadata as m; print('hermes-agent', m.version('hermes-agent'))"

cat <<EOF

Next steps (see docs/vector/herald-upgrade-runbook.md):
  1. Back up the profile (config, state.db via sqlite3 .backup, plist).
  2. Render scripts/vector/ai.hermes.gateway-vector.plist.template with
     RUNTIME=$RUNTIME_DIR and PROFILE_HOME=$PROFILE_HOME, install to
     ~/Library/LaunchAgents/ai.hermes.gateway-vector.plist.
  3. launchctl bootout gui/\$UID/ai.hermes.gateway-vector || true
     launchctl bootstrap gui/\$UID ~/Library/LaunchAgents/ai.hermes.gateway-vector.plist
EOF
