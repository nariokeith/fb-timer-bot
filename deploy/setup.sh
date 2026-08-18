#!/usr/bin/env bash
# First-time provisioning for a fresh Ubuntu VM (Oracle Cloud or any VPS).
#
# Run once, as a sudo-capable user, from anywhere:
#   curl -fsSL <raw-url>/deploy/setup.sh | bash
# or, after cloning:  sudo bash deploy/setup.sh
#
# Idempotent: safe to re-run. It will not overwrite an existing .env.
set -euo pipefail

APP_DIR=/opt/fb-timer-bot
APP_USER=ubuntu
REPO=https://github.com/nariokeith/fb-timer-bot.git

# requirements.txt pins audioop-lts, which declares requires-python >=3.13
# (audioop left the standard library in 3.13 and this is the backport).
# Ubuntu 24.04 ships 3.12, so the distro python cannot install these pins
# at all -- hence deadsnakes rather than `apt install python3`.
PYTHON=python3.13

log() { printf '\n== %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash deploy/setup.sh" >&2
  exit 1
fi

log "Installing Python ${PYTHON} and git"
apt-get update -qq
apt-get install -y -qq software-properties-common git curl
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq
apt-get install -y -qq "${PYTHON}" "${PYTHON}-venv"

log "Fetching the code into ${APP_DIR}"
if [ -d "${APP_DIR}/.git" ]; then
  git -C "${APP_DIR}" pull --ff-only
else
  mkdir -p "${APP_DIR}"
  git clone "${REPO}" "${APP_DIR}"
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

log "Building the virtualenv"
sudo -u "${APP_USER}" "${PYTHON}" -m venv "${APP_DIR}/.venv"
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

log "Installing the systemd unit"
install -m 644 "${APP_DIR}/deploy/fb-timer-bot.service" /etc/systemd/system/fb-timer-bot.service
systemctl daemon-reload
systemctl enable fb-timer-bot

if [ ! -f "${APP_DIR}/.env" ]; then
  # Deliberately not started: without credentials the bots would exit 78
  # and the supervisor would leave them stopped, which looks like a
  # failure rather than the "you still have a step to do" that it is.
  cat >&2 <<MSG

== Almost done.

  No ${APP_DIR}/.env yet, so the service is enabled but NOT started.

  1. Create it (copy the values from your Render dashboard):

       sudo -u ${APP_USER} nano ${APP_DIR}/.env
       sudo chmod 600 ${APP_DIR}/.env

     It needs, one per line, KEY=value:
       DISCORD_TOKEN, ATTENDANCE_DISCORD_TOKEN, ITEMS_DISCORD_TOKEN,
       SHEET_ID, ITEMS_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON,
       GEMINI_API_KEY, BOT_TZ=Asia/Manila, ITEMS_GEAR_DAILY_CAP=3

     GOOGLE_SERVICE_ACCOUNT_JSON must be the whole key on ONE line.

  2. Suspend the Render service, so two copies do not both post.

  3. Start:

       sudo systemctl start fb-timer-bot
       journalctl -u fb-timer-bot -f

MSG
  exit 0
fi

log "Starting"
systemctl restart fb-timer-bot
sleep 5
systemctl --no-pager --lines=20 status fb-timer-bot || true
