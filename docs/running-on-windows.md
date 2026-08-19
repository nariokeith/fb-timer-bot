# Running the bots on a Windows PC

**Why here:** Render's free tier routes every service in a region through
an address shared with every other customer there, and Discord refused it
from Oregon, Singapore and Ohio in turn. Oracle's signup rejected the
account; Google Cloud's requires tax information. A machine at home is
what is left — and it is the option with the best evidence: on 2026-08-18
all three bots ran on a home machine without a single block while Render
was being refused outright. Discord treats residential addresses far more
gently than datacentre ranges.

## The bots run inside WSL2, not on Windows directly

`bot.py` calls `time.tzset()`, which Python documents as Unix-only. On
native Windows the timer bot crashes on import, and forcing Asia/Manila
would instead depend silently on the PC's own clock being set to it.

WSL2 gives a real Ubuntu, so `deploy/setup.sh` and the systemd unit apply
unchanged. That means one deployment path to keep working rather than a
second Windows-only one that nobody would ever run and nobody would
notice rotting.

## Before you start

The `.env` you are about to copy onto someone else's PC holds three
Discord bot tokens, a Google service account key with **write access to
the Logs Tracker and Point System sheets**, and a **billable** Gemini API
key. File permissions do not protect it from the machine's owner.

Give it only to someone you would trust with the guild's records. Discord
tokens can be regenerated at any time if that changes; put a quota cap on
the Gemini key, because that one can cost real money.

## 1. Install WSL2

In PowerShell **as Administrator**:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot. On first launch Ubuntu asks for a username and password — any are
fine, but remember them; `setup.sh` installs the service under that user.

## 2. Turn on systemd

WSL2 ships with systemd off, and without it `setup.sh` has nothing to
enable. Inside Ubuntu:

```bash
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
```

Then back in PowerShell:

```powershell
wsl --shutdown
```

Reopen Ubuntu and confirm with `systemctl is-system-running` — `running`
or `degraded` both mean systemd is up.

## 3. Install the bots

Inside Ubuntu, exactly as on any VPS:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/nariokeith/fb-timer-bot.git /tmp/fb
sudo bash /tmp/fb/deploy/setup.sh
```

Run it with `sudo` from your own shell, not as root directly — that is
how the script learns which user to run the service as.

## 4. Credentials

`setup.sh` stops before starting, because a credential-less start exits 78
and reads as a crash rather than a remaining step.

```bash
sudo nano /opt/fb-timer-bot/.env
sudo chmod 600 /opt/fb-timer-bot/.env
```

Nine values, copied from Render → Environment:

```
DISCORD_TOKEN=...
ATTENDANCE_DISCORD_TOKEN=...
ITEMS_DISCORD_TOKEN=...
SHEET_ID=...
ITEMS_SHEET_ID=...
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
GEMINI_API_KEY=...
BOT_TZ=Asia/Manila
ITEMS_GEAR_DAILY_CAP=3
```

`GOOGLE_SERVICE_ACCOUNT_JSON` must be the entire key on **one line**. A
pasted multi-line key is not valid JSON and fails with "Service account
JSON is not valid JSON" — this cost an hour on 2026-08-18.

## 5. Autostart

WSL2 does not boot on its own, so nothing would come back after a
restart. In PowerShell, from the repo:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1
```

That registers a logon task which starts the distro; systemd inside it
starts the supervisor, which starts the three bots. To undo it:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1 -Stop
```

## 6. Cut over

Suspend the Render service **first**. Two live copies double-post every
boss warning and both write the same pinned state messages.

```bash
sudo systemctl start fb-timer-bot
journalctl -u fb-timer-bot -f
```

Success looks like:

```
[supervisor] keep-alive listening on port 8080
[supervisor] starting timer: /opt/fb-timer-bot/.venv/bin/python -u bot.py
Logged in as M2 TIMER#9367
[items] logged in as Ukay-Ukay sa Bahay ni Talong#6513
Attendance bot logged in as BK Attendance#8249
[items] restored state from remembered #item-distribution
State restored from Discord: True
```

No data migration. All state lives in pinned Discord messages, so the
queue, the timers and any raffle session restore on first login.

Then delete the Render service, the HetrixTools monitor and the DuckDNS
domain — all three existed only to stop Render sleeping.

## Day-to-day

From PowerShell:

```powershell
wsl -d Ubuntu-24.04 -- journalctl -u fb-timer-bot -f
wsl -d Ubuntu-24.04 -- sudo systemctl restart fb-timer-bot
wsl -d Ubuntu-24.04 -- sudo bash /opt/fb-timer-bot/deploy/update.sh
```

There is no auto-deploy. Pushing to `main` changes nothing until
`update.sh` runs — a deliberate trade: the 2026-08-18 outage was made
worse by an auto-deploy landing mid-rate-limit and restarting all three
bots into it.

## The honest limits

**Sleep is the one thing WSL cannot paper over.** A sleeping PC runs no
bots, and Windows sleeps by default. Set Power & battery → Screen and
sleep → **When plugged in, put my device to sleep after: Never**. The
screen may sleep; the machine may not.

**Fast Startup can leave WSL down after a shutdown.** If the bots do not
come back after a full power-off, turn off Fast Startup in Control Panel
→ Power Options → Choose what the power buttons do.

**It stops when the machine does.** No power, no bot — and the guild has
no visibility into that, so agree with whoever owns the PC that they will
say something if they take it offline for a while.

**Windows Update reboots.** The logon task covers reboots that reach the
login screen and get logged into. A PC left at the lock screen after an
overnight update will not start the bots until someone signs in.
