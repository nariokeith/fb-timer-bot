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

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# put DISCORD_TOKEN=... in .env
.venv/bin/python bot.py
```

Requires the **Message Content Intent**: [Developer Portal](https://discord.com/developers/applications) → your app → **Bot** → **Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT** → Save.

⚠️ Never run the local bot and the Render bot at the same time — you'd get double notifications.

## Deploy 24/7 on Render (free tier)

Free-tier services sleep after 15 minutes without web traffic, so the bot runs a tiny web server and an external pinger keeps it awake. State is mirrored to a pinned Discord message because Render wipes the disk on every restart.

1. **Push this repo to GitHub** (private is fine; `.env` is git-ignored and never uploaded).
2. **Create the service:** [dashboard.render.com](https://dashboard.render.com) → **New → Blueprint** → connect the GitHub repo. Render reads `render.yaml` and configures everything; it will prompt you for **DISCORD_TOKEN** — paste your bot token there.
   - (Alternative without Blueprint: New → Web Service → pick the repo → runtime Python, build `pip install -r requirements.txt`, start `python -u bot.py`, plan Free → add env vars `DISCORD_TOKEN`, `BOT_TZ=Asia/Manila`, `PYTHON_VERSION=3.13.4`.)
3. Wait for the deploy, then open the service URL (like `https://fb-timer-bot.onrender.com`) — it should say *FB Timer bot is alive*, and the bot should be online in Discord.
4. **Keep it awake:** create a free monitor at [uptimerobot.com](https://uptimerobot.com) → **New Monitor** → type *HTTP(s)* → paste the Render URL → interval **5 minutes**.
5. In Discord, run `!setchannel` in your notification channel. Optionally run `!setstoragechannel` in a private channel to keep the storage message out of sight.

Auto-deploy is on by default: pushing to GitHub redeploys the bot. Kill timers survive redeploys via the pinned storage message.

### Free-tier fine print

- 750 free instance hours/month covers one service 24/7 — but only one; a second free service would exceed it.
- If the pinger ever lapses and the instance sleeps, a notification can arrive late or (rarely) be missed while it wakes.
