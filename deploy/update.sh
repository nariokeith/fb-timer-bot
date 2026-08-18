#!/usr/bin/env bash
# Pull the latest code and restart. This is the VPS equivalent of Render's
# auto-deploy -- run it after pushing to main.
set -euo pipefail

APP_DIR=/opt/fb-timer-bot

git -C "${APP_DIR}" pull --ff-only
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
sudo systemctl restart fb-timer-bot

sleep 5
sudo systemctl --no-pager --lines=20 status fb-timer-bot
