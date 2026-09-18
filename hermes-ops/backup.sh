#!/usr/bin/env bash
# hermes-ops/backup.sh — snapshot curated persistent Hermes state (immutable, hash-verified).
# Storage env (any alias set; B2 preferred): B2_* | S3_* | R2_* — via creds env, never CLI.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
require_any(){ for a in "$@"; do if [ -n "${!a:-}" ]; then return 0; fi; done; echo "missing storage env: one of $*" >&2; exit 2; }
require_any B2_ENDPOINT S3_ENDPOINT R2_ENDPOINT
require_any B2_BUCKET S3_BUCKET R2_BUCKET
require_any B2_APPLICATION_KEY_ID S3_ACCESS_KEY_ID R2_ACCESS_KEY_ID
require_any B2_APPLICATION_KEY S3_SECRET_ACCESS_KEY R2_SECRET_ACCESS_KEY
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_VERSION="${HERMES_VERSION:-unknown}"
export HERMES_COMMIT="${HERMES_COMMIT:-unknown}"
export HERMES_ENV_ID="${HERMES_ENV_ID:-$(hostname)}"
exec python3 -m hermes_persist backup --home "$HERMES_HOME" --env-id "$HERMES_ENV_ID" </dev/null
