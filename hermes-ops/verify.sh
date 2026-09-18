#!/usr/bin/env bash
# hermes-ops/verify.sh — verify snapshot hashes/manifest. Exit 3 on integrity failure.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
require_any(){ for a in "$@"; do if [ -n "${!a:-}" ]; then return 0; fi; done; echo "missing storage env: one of $*" >&2; exit 2; }
require_any B2_ENDPOINT S3_ENDPOINT R2_ENDPOINT
require_any B2_BUCKET S3_BUCKET R2_BUCKET
require_any B2_APPLICATION_KEY_ID S3_ACCESS_KEY_ID R2_ACCESS_KEY_ID
require_any B2_APPLICATION_KEY S3_SECRET_ACCESS_KEY R2_SECRET_ACCESS_KEY
exec python3 -m hermes_persist verify "$@" </dev/null
