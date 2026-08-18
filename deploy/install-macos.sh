#!/usr/bin/env bash
# Install the bots as a launchd agent on this Mac.
#
#   bash deploy/install-macos.sh          # install and start
#   bash deploy/install-macos.sh --stop   # stop and uninstall
#
# Idempotent: re-running reinstalls cleanly.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL=com.fbtimer.supervisor
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [ "${1:-}" = "--stop" ]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "${PLIST}"
  echo "Stopped and removed ${LABEL}."
  exit 0
fi

if [ ! -x "${APP_DIR}/.venv/bin/python" ]; then
  echo "No virtualenv at ${APP_DIR}/.venv -- create it first:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [ ! -f "${APP_DIR}/.env" ]; then
  echo "No ${APP_DIR}/.env -- the bots would exit 78 and stay stopped." >&2
  exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents" "${APP_DIR}/logs"
sed "s|__APP_DIR__|${APP_DIR}|g" "${APP_DIR}/deploy/${LABEL}.plist" > "${PLIST}"

# bootout first so a re-run replaces the old definition rather than
# failing with "service already loaded".
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"

echo "Installed and started ${LABEL}."
echo
echo "  Follow the log:  tail -f ${APP_DIR}/logs/supervisor.log"
echo "  Stop:            bash deploy/install-macos.sh --stop"
echo
echo "Remember to suspend the Render service, or both copies will post."
