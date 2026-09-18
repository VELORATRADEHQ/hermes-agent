#!/usr/bin/env bash
# hermes-ops/install-service.sh — install/enable the systemd USER unit (+ linger) when systemd
# is available; otherwise print the non-systemd supervisor alternative. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="hermes-gateway.service"
TARGET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DEFAULT="$HOME/harea/venv/bin/hermes"
HERMES_BIN="${HERMES_BIN:-$BIN_DEFAULT}"

if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user >/dev/null 2>&1; then
  echo "systemd --user not available on this host."
  echo "Fallback: hermes-ops/gateway-supervisor.sh (exec via init/container entry or cron @reboot)."
  exit 3
fi

mkdir -p "$TARGET_DIR"
sed "s|%h/harea/venv/bin/hermes|$HERMES_BIN|g" "$HERE/$UNIT" > "$TARGET_DIR/$UNIT"
[ -e "$HOME/.hermes/creds.env" ] || { echo "ERROR: ~/.hermes/creds.env missing (secrets env file)"; exit 2; }
chmod 600 "$HOME/.hermes/creds.env" || true
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" || echo "warn: loginctl enable-linger failed (needs systemd-logind)"
fi
echo "installed: $UNIT (user), watchdog 90s, restart on-failure, burst-capped"
systemctl --user status "$UNIT" --no-pager -l | sed -E 's/(TOKEN|KEY|SECRET)=[^ ]+/\1=***/g' | head -12
