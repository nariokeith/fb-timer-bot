# Moving the bots to Oracle Cloud (or any VPS)

Why: Render's free tier routes every service in a region through a shared
outbound IP ("Outbound IP ranges are shared across *all* services in the
same region" — Render's own docs). On 2026-08-18 that address was refused
by Discord twice in one day: a Cloudflare 1015 edge ban in the morning
and an application-layer global rate limit in the evening that lasted
over 40 minutes. Neither was caused by this bot's traffic, and no code
change can lift them.

A VPS gives you an address nobody else shares. It also deletes most of
what exists here purely to survive Render's free tier:

| Piece                        | On Render | On a VPS |
|------------------------------|-----------|----------|
| Spin-down after 15 min idle  | yes       | never    |
| keep-alive HTTP server       | required  | vestigial (harmless) |
| self-ping                    | required  | switches itself off* |
| HetrixTools / UptimeRobot    | required  | not needed |
| DuckDNS domain               | required  | not needed |
| 750 instance-hours/month cap | yes       | none     |

\* `start_self_ping()` keys off `RENDER_EXTERNAL_URL`, which only Render
sets, so it disables itself with no configuration. `tests/test_deploy.py`
pins that.

## 1. Create the instance

Oracle Cloud → Compute → Instances → **Create instance**.

- **Shape:** `VM.Standard.A1.Flex` (Ampere ARM) — Always Free covers 4
  OCPU / 24 GB across your tenancy; 1 OCPU / 6 GB is ample here. If Oracle
  says "out of capacity" (common), take `VM.Standard.E2.1.Micro` instead:
  1 OCPU / 1 GB, which still fits three bots at roughly 150 MB each.
- **Image:** Ubuntu 24.04 LTS.
- **Region:** Singapore, closest to Asia/Manila players.
- **SSH keys:** upload your public key; Oracle does not set a password.

No ingress rules are needed. The bots make only outbound connections, and
the keep-alive port stays firewalled — which is fine, nothing external
needs it any more.

Note the instance's **public IP**. That is the address Discord will now
see, and it is yours alone.

## 2. Provision

```bash
ssh ubuntu@<public-ip>
git clone https://github.com/nariokeith/fb-timer-bot.git /tmp/fb
sudo bash /tmp/fb/deploy/setup.sh
```

`setup.sh` installs Python 3.13 from the deadsnakes PPA, clones to
`/opt/fb-timer-bot`, builds the venv, installs the pins and enables the
systemd unit. It is idempotent — re-running it is safe.

**Python 3.13 specifically, not the distro's python3.** `requirements.txt`
pins `audioop-lts`, which declares `requires-python >= 3.13` (audioop left
the standard library in 3.13; this is the backport discord.py needs).
Ubuntu 24.04 ships 3.12, so the distro interpreter cannot install these
pins at all — the failure would land after the VM was already built.

## 3. Credentials

The script stops before starting, because a credential-less start exits 78
and looks like a crash rather than a missing step.

```bash
sudo -u ubuntu nano /opt/fb-timer-bot/.env
sudo chmod 600 /opt/fb-timer-bot/.env
```

Copy the values from Render → BK BOT → Environment:

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

## 4. Cut over

Suspend the Render service **first**. Two live copies would double-post
every boss warning and both write the same pinned state messages.

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
[items] restored state from #item-distribution
State restored from Discord: True
```

No data migration: all three bots keep their state in pinned Discord
messages, so the queue, the timers and any raffle session restore on the
first successful login.

Once you are happy, delete the Render service.

## Day-to-day

```bash
sudo bash /opt/fb-timer-bot/deploy/update.sh   # after pushing to main
journalctl -u fb-timer-bot -f                  # follow the logs
journalctl -u fb-timer-bot --since "1 hour ago" | grep -i "rate"
sudo systemctl restart fb-timer-bot
sudo systemctl stop fb-timer-bot
```

There is no auto-deploy. Pushing to `main` changes nothing until you run
`update.sh` — a deliberate trade: today's outage was made worse by an
auto-deploy landing in the middle of a rate limit and restarting all three
bots into it.

## Running it on a Mac instead

Every VPS worth using -- Oracle's free tier included -- asks for a credit
card to verify the account. Without one, the remaining option that solves
the shared-IP problem is your own hardware, and it is the option with the
best evidence: on 2026-08-18 the bots ran on a home Mac for fifteen
minutes without a single block, while Render was being refused outright.

```bash
bash deploy/install-macos.sh          # install and start
tail -f logs/supervisor.log           # watch it
bash deploy/install-macos.sh --stop   # uninstall
```

That installs a launchd agent which starts at login and restarts the
supervisor if it exits. It runs under `caffeinate -i`, so the Mac will not
doze off between boss spawns.

What it cannot do:

- **A closed laptop lid still sleeps.** `caffeinate -i` holds off *idle*
  sleep only. For genuine 24/7 the Mac needs its lid open, or an external
  display, or to be a desktop.
- **It stops when the machine does.** Power cuts and reboots need someone
  to log back in, since a launchd *agent* starts at login rather than at
  boot.

Suspend the Render service before starting this, or both copies will post
every boss warning twice and fight over the same pinned state messages.

## What this does not fix

Discord treats datacentre address ranges more harshly than residential
ones, so a dedicated VPS IP is *likely* but not *certain* to end the
blocks. The only environment with a perfect record on 2026-08-18 was a
home machine. If Oracle's address is also refused, a Raspberry Pi at home
is the fallback with actual evidence behind it.
