#!/usr/bin/env bash
# hermes-ops/owner-onboard.sh — first-run OWNER/ADMIN setup (numeric Telegram user id only).
# Writes (never prints) allow_admin_from:[<id>] into config.yaml extra and appends the id
# to TELEGRAM_ALLOWED_USERS in ~/.hermes/.env (0600) if missing.
# Usage: ./owner-onboard.sh   (interactive)   |   ./owner-onboard.sh 123456789
set -euo pipefail
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CFG="$HERMES_HOME/config.yaml"
ENVF="$HERMES_HOME/.env"
uid="${1:-}"
if [ -z "$uid" ]; then
  read -rp "Enter the Telegram numeric user ID that should become the Hermes owner/admin: " uid
fi
if ! [[ "$uid" =~ ^[0-9]+$ ]]; then
  echo "ERROR: owner id must be numeric. Aborting." >&2; exit 2
fi
if [ ${#uid} -lt 3 ] || [ ${#uid} -gt 20 ]; then
  echo "ERROR: implausible numeric id length. Aborting." >&2; exit 2
fi
mkdir -p "$HERMES_HOME"
touch "$CFG"
python3 - "$CFG" "$uid" <<'PY'
import sys
from pathlib import Path
cfg_path, uid = Path(sys.argv[1]), sys.argv[2]
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required (use the Hermes venv python).", file=sys.stderr); raise SystemExit(2)
raw = cfg_path.read_text()
cfg = yaml.safe_load(raw) if raw.strip() else {}
if not isinstance(cfg, dict): cfg = {}
plat = cfg.setdefault("gateway", {}).setdefault("platforms", {}).setdefault("telegram", {})
extra = plat.setdefault("extra", {})
admins = extra.get("allow_admin_from", [])
if not isinstance(admins, list): admins = [admins]
if str(uid) not in {str(a) for a in admins}: admins.append(int(uid))
extra["allow_admin_from"] = admins
cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
PY
touch "$ENVF"; chmod 600 "$ENVF"
if grep -q '^TELEGRAM_ALLOWED_USERS=' "$ENVF"; then
  if ! grep -Eq "^TELEGRAM_ALLOWED_USERS=([0-9,]*\b)?$uid\b" "$ENVF"; then
    sed -i "s|^TELEGRAM_ALLOWED_USERS=\(.*\)$|TELEGRAM_ALLOWED_USERS=\1,$uid|" "$ENVF"
  fi
else
  echo "TELEGRAM_ALLOWED_USERS=$uid" >> "$ENVF"
fi
echo "owner-onboard OK (numeric id stored; value not echoed)"
