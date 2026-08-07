# Lordnine: Infinite Class — Field Boss Timer Bot

Discord bot that tracks all 40 field bosses (spawn data hardcoded from `Fieldboss_Timer.md`) and posts a notification **10 minutes before** every spawn. Built to run 24/7 on Render's free tier.

Two kinds of bosses:

- **Interval bosses** (22) — respawn a fixed number of hours after being killed (e.g. Supore = 62h). You record the kill time; the bot computes the next spawn.
- **Scheduled bosses** (18) — spawn at fixed weekly day/time slots (e.g. Clemantis = Monday 11:30 and Thursday 19:00). Nothing to record; the bot always knows the next one.

## Commands

| Command | What it does |
|---|---|
| `!setchannel` | Run once in the channel where spawn notifications should go. **Required.** Unless you set a storage channel, the bot also pins a small storage message here — don't delete it; it's how timers survive restarts. |
| `!setstoragechannel` | Optional. Run in a private channel to move the storage message there, keeping the notification channel clean. Existing timers move across automatically — nothing has to be re-entered. The bot needs View Channel, Send Messages and Read Message History there (Manage Messages too, so it can pin). |
| `!clearstoragechannel` | Store the timers back in the notification channel. |
| `!killed <boss> [time]` | Record an interval boss kill. Time is optional (defaults to right now). Examples: `!killed Supore` · `!killed Supore 9PM` · `!killed venatus 21:30` · `!killed Ordo 2026-07-20 21:00`. A time later than now is assumed to be yesterday. |
| `!boss <name>` | Show one boss's next spawn. Names are case-insensitive and prefix-matched (`!boss sup` → Supore). |
| `!bosses` | List all 40 bosses sorted by next spawn (unknown/overdue at the bottom). |
| `!timer <seconds>` | Simple live countdown (max 3600s). |

All schedule times and typed kill times ("9PM") are interpreted in the `BOT_TZ` timezone (**Asia/Manila** by default), regardless of where the server runs. Displayed times use Discord timestamps, so everyone sees their own timezone.

**Attendance bot** (a separate bot — see below):

| Command | What it does |
|---|---|
| `!attendance <boss>` | Officers only. Attach one or more in-game roster screenshots; the bot reads every image, merges the names and adds that boss's points to the Point System sheet. Shows a preview first — nothing is written until an officer reacts ✅. |
| `!attendance <boss> - <boss> - <boss>` | Same, for a rally that killed several bosses with one roster, e.g. `!attendance clemantis - dalia - catena`. Each boss gets its own point value, all in one write, and one `!undoattendance` reverses the lot. |
| `!undoattendance` | Officers only. Reverses the most recent attendance log. |
| `!setweek <tab>` | Officers only. Sets which sheet tab attendance goes into, e.g. `Week 17.1`. |
| `!setofficerrole @role [@role ...]` | Admins only. Sets every role that may record attendance, replacing the current set. |
| `!attendancehelp` | Lists the attendance commands. |

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# put DISCORD_TOKEN=... in .env
.venv/bin/python bot.py
```

Requires the **Message Content Intent**: [Developer Portal](https://discord.com/developers/applications) → your app → **Bot** → **Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT** → Save.

⚠️ Never run the local bot and the Render bot at the same time — you'd get double notifications.

## Attendance logging

Attendance points live in the *Point System* Google Sheet: one row per
player, one column per boss, and a cell value that accumulates points.
Most bosses are worth 1 point per attendance; `Lucus`, `Libitina`,
`Rakajeth`, `Icaruthia`, `Motti`, `Nevaeh`, `Tumier` and `Camalia` are
worth 3 (each is annotated `- 3` in the sheet itself).

Boss names resolve from the target tab's header row by exact match, then
unique prefix, then unique substring — so `!attendance dalia` finds
*Lady Dalia*. `Venatus` and `Viorent` deliberately have no attendance
column and are refused. Two in-game names are aliased in code because
they differ from the sheet: `KobePH` → `Kobe`, and `面白い` →
`chinchong ni Mumu`.

One rally often kills several bosses with the same roster, so several can
be named in one command, separated by ` - ` (space dash space):

```
!attendance clemantis - dalia - catena
```

Each boss is resolved and scored on its own, and every column is written
in a single update — so the command either logs all of them or none. If
any name fails to resolve the whole command is refused, naming the part
that failed, rather than quietly logging the rest. Repeats collapse
(`dalia - dalia` is one boss), and one `!undoattendance` reverses the
whole submission together. Commas are not separators, and plain spaces
cannot be, because names like `Lady Dalia` and `General Aquleus` contain
spaces. Note that the sheet annotates point values with the same ` - `,
so `!attendance lucus - 3` is read as two bosses and refused on the `3`.

### Two bots, one service

Attendance is a **separate Discord bot** with its own token and its own
code. It never imports `bot.py`. `supervisor.py` starts both as
independent processes, so a crash or a bad deploy on the attendance side
cannot stop the timer.

They share one Render service deliberately. Render's 750 free instance
hours are shared across a workspace, so two services running 24/7 would
exhaust them around the 16th of each month — and Render then suspends
*every* free service, timer included.

Be honest about what "separate" buys here: the two bots are isolated as
**processes** (crash, exception, import error, blocked event loop), not
as **resources**. Both run inside the same 512 MB / 0.1 CPU instance, so
an attendance-side memory leak or runaway process can still starve or OOM
the whole instance and take the timer down with it. `supervisor.py`'s
restart backoff limits how often a broken child can chew CPU, but it
cannot give the timer its own memory budget.

**This makes the feature safe to deploy before its credentials exist.**
If `ATTENDANCE_DISCORD_TOKEN`, `SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`
or `GEMINI_API_KEY` is missing, `attendance_bot.py` exits immediately
(code 78) and `supervisor.py` leaves that child stopped — the timer
keeps running normally either way.

### Setup

1. **Second Discord application** — at the
   [Developer Portal](https://discord.com/developers/applications), create a
   new application and bot, enable **MESSAGE CONTENT INTENT**, and invite it
   to your server. Its token becomes `ATTENDANCE_DISCORD_TOKEN`.
2. **Gemini API key** — create one at
   [aistudio.google.com/apikey](https://aistudio.google.com/apikey). No credit
   card, no billing account; the free tier allows 1,000 requests a day. Note
   that on the free tier Google may use submitted content to improve its
   products.
3. **Service account** — in [Google Cloud Console](https://console.cloud.google.com),
   create a project, enable the **Google Sheets API** on it, create a service
   account, and download its JSON key. The Sheets API step is easy to miss
   and produces a confusing failure if skipped.
4. **Share the sheet** — open the Point System sheet, press Share, and give
   the service account's `client_email` (it ends in
   `.iam.gserviceaccount.com`) **Editor** access. Without this the bot
   cannot see the sheet at all.
5. **Set the env vars** on Render: `ATTENDANCE_DISCORD_TOKEN`,
   `GEMINI_API_KEY`, `SHEET_ID` (the long ID in the sheet's URL) and
   `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole JSON key, pasted as one line).
6. **In Discord**, run `!setofficerrole @Officer` then `!setweek "Week 17"`.

The bot creates two hidden tabs in the sheet on first use — don't delete
them:

- `_BotConfig` holds the target tab and officer role, so they survive
  Render restarts.
- `_BotLog` records one row per confirmed submission; it's what makes
  `!undoattendance` and duplicate-screenshot detection work. Duplicate
  detection is keyed on a hash of the screenshot's own image bytes
  (`image_sha256`), not on the Discord attachment id — Discord mints a
  new id on every upload, so id-based detection would never catch a
  genuine re-post of the same picture.

  If `_BotLog`'s column layout (`LOG_HEADER` in `attendance_sheet.py`)
  is ever changed in code, the bot will refuse to read or write an
  **existing** `_BotLog` tab whose row 1 doesn't match — on purpose,
  because silently re-zipping old rows against a new column order would
  misattribute fields and make undo subtract the wrong points from the
  wrong people. There is no automatic migration: fix row 1 by hand (or
  delete the tab and let the bot recreate it, losing history) after a
  `LOG_HEADER` change.

### Testing locally

⚠️ **Stop the Render instance first** (or point this local run at a
different `SHEET_ID`, e.g. a scratch copy of the sheet). `attendance_sheet.py`'s
locking (`asyncio.Lock` plus a read-then-write pattern) is per-**process**,
not per-sheet: a local run and the Render instance are two independent
processes with two independent locks, and neither can see the other's
lock. Both would happily read the same cell's current value at once and
both write their own answer — one attendance entry silently vanishes,
with no error from either side. This is the same hazard `attendance_bot.py`'s
own `_SHEET_LOCK` protects against *within* one process; it does nothing
across two.

Because the attendance bot is a **separate Discord application with its
own token**, it can be run standalone with the production timer completely
uninvolved:

```bash
export ATTENDANCE_DISCORD_TOKEN="..." SHEET_ID="..." GEMINI_API_KEY="..."
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"
.venv/bin/python attendance_bot.py
```

No duplicate boss notifications and nothing to stop first — `bot.py` and
`attendance_bot.py` use different tokens, so both can be online at once.

Point `SHEET_ID` at the *Point System* sheet and use the `bot test` tab
(`!setweek "bot test"`), not `Week 17` or any real week — it's structurally
identical (same 57×40 layout, the same 38 boss columns, the same 49
players) and exists specifically so this checklist can be run against real
data without touching a live tab.

Checklist:

1. The bot comes online in Discord.
2. `!attendancehelp` lists the five commands.
3. `!setofficerrole @Officer`, then `!setweek "bot test"`.
4. A non-officer running `!attendance Lucus` is refused.
5. `!attendance lucus` with a real screenshot shows a preview; check the
   matched names against the screenshot by eye. Posting two screenshots
   on the one message reads both, and the preview says how many names
   came from each.
6. React ✅ → the `bot test` tab's `Lucus` column gains 3 for each matched
   player, and column B recalculates itself.
7. Re-post the same screenshot → the preview carries the "Already Logged"
   warning.
8. `!undoattendance` → the points come back off and `_BotLog` shows
   `reversed`.
9. `!attendance clemantis - dalia` → the preview lists both bosses with
   their own point values and the per-player total; confirming writes
   both columns at once, and one `!undoattendance` takes both back off.

Two fail-closed behaviours are deliberate, not bugs — expect them:

- If any value other than blank/`yes`/`true`/`1` ever appears in
  `_BotLog`'s `reversed` column, both `!attendance` and `!undoattendance`
  refuse until it is corrected. The error names the exact row and value.
- A boss cell containing anything that isn't a whole number (an `x`, a
  note, a formula, `1,000`, `3.7`) makes the bot refuse rather than
  overwrite it. The error names the cell.

## Item distribution

Members request items from the separate item-distribution bot:

| Command | What it does |
|---|---|
| `!request <item name> <IGN>` | Request an item, for example `!request Asta's Heart Kobe`. |
| `!myrequests` | List your pending requests. |
| `!cancelrequest [item name]` | Withdraw a pending request; name the item when you have more than one. |

Two rules are enforced from the spreadsheet: a special log can be received
once per player, ever; gear logs are limited to three per player per day,
resetting at midnight in Manila time. An IGN must match the player's row in
the Logs Tracker sheet.

An administrator must first run `!setofficerchannel` in the private officer
channel. That channel is where officers run `!distribute` and approve or deny
requests; the bot stores its pending queue in a pinned message there.

Create a third Discord application and enable **Message Content Intent** for
its bot. Invite it with **View Channels**, **Send Messages**, **Embed Links**,
**Read Message History**, and **Manage Messages** permissions. Share the Logs
Tracker spreadsheet with the Google service account's `client_email` as an
**Editor**. On Render, set `ITEMS_DISCORD_TOKEN`, `ITEMS_SHEET_ID`, and
`GOOGLE_SERVICE_ACCOUNT_JSON`; `ITEMS_GEAR_DAILY_CAP` is optional and defaults
to `3`.

## Deploy 24/7 on Render (free tier)

Free-tier services sleep after 15 minutes without web traffic, so the bot runs a tiny web server and an external pinger keeps it awake. State is mirrored to a pinned Discord message because Render wipes the disk on every restart.

1. **Push this repo to GitHub** (private is fine; `.env` is git-ignored and never uploaded).
2. **Create the service:** [dashboard.render.com](https://dashboard.render.com) → **New → Blueprint** → connect the GitHub repo. Render reads `render.yaml` and configures everything; it will prompt you for **DISCORD_TOKEN** (plus the four attendance secrets, which can be left blank for now — see [Attendance logging](#attendance-logging)) — paste your bot token there.
   - (Alternative without Blueprint: New → Web Service → pick the repo → runtime Python, build `pip install -r requirements.txt`, start `python -u supervisor.py`, plan Free → add env vars `DISCORD_TOKEN`, `BOT_TZ=Asia/Manila`, `PYTHON_VERSION=3.13.4`, plus `ATTENDANCE_DISCORD_TOKEN`, `SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GEMINI_API_KEY` if attendance is being set up now.)
3. Wait for the deploy, then open the service URL (like `https://fb-timer-bot.onrender.com`) — it should say *FB Timer bot is alive*, and the bot should be online in Discord.
4. **Keep it awake:** create a free monitor at [uptimerobot.com](https://uptimerobot.com) → **New Monitor** → type *HTTP(s)* → paste the Render URL → interval **5 minutes**.
5. In Discord, run `!setchannel` in your notification channel. Optionally run `!setstoragechannel` in a private channel to keep the storage message out of sight.

Auto-deploy is on by default: pushing to GitHub redeploys the bot. Kill timers survive redeploys via the pinned storage message.

### Free-tier fine print

- 750 free instance hours/month covers one service 24/7 — but only one; a second free service would exceed it.
- If the pinger ever lapses and the instance sleeps, a notification can arrive late or (rarely) be missed while it wakes.
