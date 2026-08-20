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
3. Wait — a few minutes, once

That is the whole of it. **No administrator, no reboot, no Python to
install, no PowerShell, no typing, nothing to configure.** Everything the
bots need lands in one folder inside their own user profile, and
uninstalling is deleting that folder plus the scheduled task.

Python comes from the **embeddable distribution**: a 10 MB zip that runs
where it is unpacked. Nothing else on their PC changes — no PATH edit, no
system Python, no interference with anything they already have.
Installing Python properly was the biggest failure point in earlier
versions of this, because it wants winget or a downloaded installer,
rewrites PATH, and can need a reboot before that PATH is visible — none
of which someone can be walked through by message.

Every package has a prebuilt Windows wheel — verified across the whole of
`requirements.txt` with `pip download --platform win_amd64
--only-binary=:all:` — so no compiler is needed either.

The installer refuses early and says why, rather than half-finishing:

- no `.env` beside it, or a credential missing from it, and it names which
- a service account key that is not valid JSON on one line
- the bots failing to import once installed, which catches a bad token or
  a broken key while the message can still be read

If anything fails it writes **`install-log.txt`** next to the .bat and
says to send it back. Nobody else can see that machine, so that file is
the only diagnostic there is — worth telling them to send it without
being asked.

## Why native, not WSL

`bot.py` used to force Asia/Manila with `os.environ["TZ"]` plus
`time.tzset()`. tzset is Unix-only, so the timer crashed on import under
Windows and WSL2 looked mandatory — which would have meant an elevated
PowerShell, a reboot and a second run, none of which someone can be
talked through blind.

Times are now anchored to `BOT_TZ` at each conversion with `ZoneInfo`
(`bot.py`: `local_now()` and `_epoch()`), so the host's clock no longer decides
anything. It is also why `requirements.txt` pins **tzdata**: Windows
ships no IANA timezone database, so `zoneinfo` has nothing to read and
`ZoneInfo("Asia/Manila")` raises on import without it.

That mattered beyond Windows: `deaths` is persisted as Unix
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
- **The installer has never run on Windows.** It was written on a Mac.
  What has been checked there: it parses under PowerShell 7.6.5, every
  cmdlet it calls ships with Windows 10/11, its `.env` parsing and
  credential checks execute correctly, and its edit to the embedded
  Python's `._pth` was run against the real file from python.org and is
  idempotent. What cannot be checked from a Mac: the scheduled task,
  `powercfg`, and the bots actually starting. `install-log.txt` is how
  the first run gets debugged.
