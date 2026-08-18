# Running the bots on a Mac

Render's free tier routes every service in a region through an outbound
address shared with every other customer there ("Outbound IP ranges are
shared across *all* services in the same region" — Render's own docs). On
2026-08-18 Discord refused that address twice in one day: a Cloudflare
1015 edge ban in the morning, and an application-layer global rate limit
in the evening that lasted over an hour. Neither followed from this bot's
traffic, and no code change lifts them.

Running on your own machine is the only free way off that address, and it
is the option with the best evidence: on the same day, all three bots ran
on a home Mac for fifteen minutes without a single block, while Render was
being refused outright.

## Install

```bash
python3 -m venv .venv                          # if you have not already
.venv/bin/pip install -r requirements.txt
bash deploy/install-macos.sh
```

The installer refuses to proceed without a `.env`, because a
credential-less start exits 78 and the supervisor leaves the bots stopped
— which reads as a crash rather than a remaining step.

**Suspend the Render service first.** Two live copies double-post every
boss warning and fight over the same pinned state messages.

## Day-to-day

```bash
tail -f logs/supervisor.log            # follow
bash deploy/install-macos.sh           # restart, e.g. after a git pull
bash deploy/install-macos.sh --stop    # uninstall
```

Success looks like:

```
[supervisor] starting timer: .../.venv/bin/python -u bot.py
Logged in as M2 TIMER#9367
[items] logged in as Ukay-Ukay sa Bahay ni Talong#6513
Attendance bot logged in as BK Attendance#8249
[items] restored state from #item-distribution
State restored from Discord: True
```

No data migration is needed. All three bots keep their state in pinned
Discord messages, so the queue, the timers and any raffle session restore
on the first successful login.

## What this drops

Everything that exists only to survive a platform that sleeps:

| Piece                        | On Render | Here |
|------------------------------|-----------|------|
| Spin-down after 15 min idle  | yes       | never |
| keep-alive HTTP server       | required  | vestigial (localhost only) |
| self-ping                    | required  | switches itself off* |
| HetrixTools / UptimeRobot    | required  | not needed |
| DuckDNS domain               | required  | not needed |
| 750 instance-hours/month cap | yes       | none |

\* `start_self_ping()` keys off `RENDER_EXTERNAL_URL`, which only Render
sets, so it disables itself with no configuration.

## The honest limits

- **A closed laptop lid still sleeps.** The agent runs under
  `caffeinate -i`, which holds off *idle* sleep only. Real 24/7 wants the
  lid open, an external display, or a desktop.
- **It starts at login, not at boot.** launchd *agents* run in a user
  session, so a power cut needs someone to log back in.
- **It stops when the machine does.** No power, no bot.

A cheap second-hand box or a Raspberry Pi left running removes all three
caveats without tying up your Mac.
