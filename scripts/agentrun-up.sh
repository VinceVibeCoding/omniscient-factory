#!/usr/bin/env bash
# Start the pinned AgentRun server or runner for Omniscient.
#
#   agentrun-up.sh server   API + console only (--no-runner)
#   agentrun-up.sh runner   the process that executes agent runs
#
# Why split: with one combined process, long Opus runs pegged the single Node thread at
# ~92% CPU and the API stopped answering. A separate runner
# keeps the API responsive. Both share the store in $AGENTRUN_DATA.
# - ipv4first: WSL has no working IPv6 route and AgentRun's guarded fetch does not fall back.
# - BROWSER=true: don't try to open a browser from WSL; open http://127.0.0.1:7340/ yourself.
set -euo pipefail

AGENTRUN_VERSION="0.4.3"
AGENTRUN_DATA="${AGENTRUN_DATA:-$HOME/.agentrun}"
MODE="${1:-server}"

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh"
nvm use 24 >/dev/null
export NODE_OPTIONS="--dns-result-order=ipv4first"
export BROWSER=true

case "$MODE" in
  server) exec npx -y "@parcha/agentrun@${AGENTRUN_VERSION}" --no-open --no-runner --data "$AGENTRUN_DATA" ;;
  runner) exec npx -y "@parcha/agentrun@${AGENTRUN_VERSION}" runner --data "$AGENTRUN_DATA" ;;
  *) echo "usage: $0 [server|runner]" >&2; exit 2 ;;
esac
