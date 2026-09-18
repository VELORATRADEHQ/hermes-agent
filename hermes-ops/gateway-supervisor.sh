#!/usr/bin/env bash
# hermes-ops/gateway-supervisor.sh — non-systemd supervisor (Deepnote/containers):
# restart-on-exit with capped backoff + periodic TELEGRAM HEALTH gate (process-alive alone
# is NEVER considered healthy). No infinite restart loop: gives up past the cap window.
# Usage: gateway-supervisor.sh -- hermes gateway --accept-hooks run
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
[ "--" = "${1:-}" ] && shift
[ $# -ge 1 ] || { echo "usage: $0 -- <gateway command...>" >&2; exit 2; }
exec python3 -m hermes_persist supervisor -- "$@"
