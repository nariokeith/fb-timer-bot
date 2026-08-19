# Running the bots on someone else's Windows PC

**Why here:** Render's free tier routes every service in a region through
an address shared with every other customer there, and Discord refused it
from Oregon, Singapore and Ohio in turn. Oracle's signup was rejected;
Google Cloud's billing needs tax information. A machine at home is what
is left — and it is the option with the best evidence: on 2026-08-18 all
three bots ran on a home machine without a single block while Render was
being refused outright. Discord treats residential addresses far more
gently than datacentre ranges.

This is written for the case where the PC belongs to someone else, who
is not technical and will not hand over remote access. **They run one
file. That is the whole of their involvement.**

## What you send them

A zip with exactly two things in it:

```
fb-timer-bot-setup.zip
├── INSTALL.bat
└── .env
```

`INSTALL.bat` is in `deploy/` in this repo, together with the
`install-windows.ps1` it calls — send all three files, or just point them
at the folder. `.env` you write yourself, from Render → Environment:

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

**Before you send it:** that file grants three Discord bot tokens, write
access to the Logs Tracker and the Point System sheets, and a *billable*
Gemini key. Put a quota cap on the Gemini key first — it is the only one
of the three that can cost money rather than cause damage you can undo.
The tokens can be regenerated at any time if you need to revoke access.

## What they do

1. Unzip the folder anywhere
2. Double-click `INSTALL.bat`
3. Wait — a few minutes the first time, mostly Python and dependencies

That is it. No administrator, no reboot, no PowerShell, no typing.

The installer downloads the code, installs Python 3.13 if it is missing,
builds the environment, copies the credentials in, registers the bots to
start at every logon, and turns off sleep-while-plugged-in. It finishes
by saying either that the bots are running or that they are not.

If anything fails it writes **`install-log.txt`** next to the .bat and
asks them to send it back. Nobody can see their screen, so that file is
the only diagnostic there is — it is worth telling them to send it
without being asked.

## Why native, not WSL

`bot.py` used to force Asia/Manila with `os.environ["TZ"]` plus
`time.tzset()`. tzset is Unix-only, so the timer crashed on import under
Windows and WSL2 looked mandatory — which would have meant an elevated
PowerShell, a reboot and a second run, none of which someone can be
talked through blind.

Times are now anchored to `BOT_TZ` at each conversion with `ZoneInfo`
(`bot.py`: `now()` and `_epoch()`), so the host's clock no longer decides
anything. That mattered beyond Windows: `deaths` is persisted as Unix
timestamps, and state written on Render (a UTC host) and read back on a
PC set to another zone silently moved every boss kill time by the offset
between them. `tests/test_bot.py` pins that.

## Cut over

**Suspend the Render service before they run the installer.** Two live
copies double-post every boss warning and both write the same pinned
state messages.

There is no data migration. All three bots keep their state in pinned
Discord messages, so the queue, the timers and any raffle session restore
on the first successful login. Once you can see the bots online and
answering, delete the Render service, the HetrixTools monitor and the
DuckDNS domain — all three existed only to stop Render sleeping.

## What to tell them, in plain words

Four things, none technical:

- **Leave the PC on and plugged in.**
- **Tell me if you turn it off for a while** — the guild has no other way
  to know the bots are down.
- **After a Windows Update restart, sign back in.** The bots start when
  you log in, so a PC sitting at the lock screen is not running them.
- **If anything looks wrong, send me `install-log.txt`.**

## Updating later

Pushing to `main` changes nothing on their PC. They re-run `INSTALL.bat`,
which reinstalls from the latest `main` and restarts the bots. It is
idempotent — re-running repairs an install rather than duplicating it.

This is deliberate rather than a missing feature: the 2026-08-18 outage
was made worse by an auto-deploy landing mid-rate-limit and restarting
all three bots into it.

## The honest limits

- **No power, no bot.** The guild has no visibility into that machine.
- **Sleep.** The installer disables sleep on AC. If they run on battery,
  or change it back, the bots stop with the machine.
- **Logon, not boot.** The task starts at logon so it never needs an
  administrator. A PC left at the lock screen after an overnight update
  will not start the bots until someone signs in.
- **The installer is untested.** It was written on a Mac, where no
  PowerShell exists to run it. `install-log.txt` is how the first run
  gets debugged.
