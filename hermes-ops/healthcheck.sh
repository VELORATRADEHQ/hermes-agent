#!/usr/bin/env bash
# hermes-ops/healthcheck.sh — secrets PRESENCE (never values) + storage + state + config check.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ARGS=("--home" "$HERMES_HOME")
if [ -z "${B2_ENDPOINT:-}${S3_ENDPOINT:-}${R2_ENDPOINT:-}" ]; then ARGS+=("--no-r2"); fi
exec python3 -m hermes_persist healthcheck "${ARGS[@]}" </dev/null
