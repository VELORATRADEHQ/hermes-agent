#!/usr/bin/env bash
# Provision Backblaze B2 for hermes_persist — official Native API, stdlib only.
#
# Requires in the CURRENT environment (never hardcoded, never printed):
#   B2_MASTER_APPLICATION_KEY_ID / B2_MASTER_APPLICATION_KEY   (account master key)
# Optional: B2_BUCKET (default hermes-state)
#
# Writes three 0600 files under ~/.hermes/state/provisioned/:
#   b2-runtime.env    Layer A  (bucket RW)
#   b2-bootstrap.env  Layer B  (readFiles, namePrefix secrets/)
#   master-key.env    HERMES_MASTER_KEY (+SECRETS_MASTER_KEY alias) — never leaves local env
# Prints metadata only (region/endpoint/paths). Key material never hits stdout/logs.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
if [ -z "${B2_MASTER_APPLICATION_KEY_ID:-}" ] || [ -z "${B2_MASTER_APPLICATION_KEY:-}" ]; then
  echo "error: B2_MASTER_APPLICATION_KEY_ID and B2_MASTER_APPLICATION_KEY must be set in the environment" >&2
  echo "(source them from your secret store; do not paste them into this script or any file that is committed)" >&2
  exit 2
fi
PY=python3
[ -x /root/harea/venv/bin/python ] && PY=/root/harea/venv/bin/python
cd "$REPO"
"$PY" -m hermes_persist provision-b2 "${@:---home=$HOME/.hermes}"
