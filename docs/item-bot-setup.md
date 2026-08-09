# Item Bot — Setup and Test Guide

Everything needed to test the item bot locally, then deploy it to Render.
Work through it in order.

---

## ⚠️ Read this first

**Never run `python supervisor.py` on your laptop.**

The supervisor starts *all three* bots. Two of them — the timer and the
attendance bot — are live on Render right now, using the same tokens that are
in your local `.env`. Starting a second copy of a bot with the same token means
**both copies answer every command**: duplicate boss timers, double attendance
writes, and a mess in your Discord that is tedious to unpick.

For local testing, always run the item bot **on its own**:

```bash
.venv/bin/python -u items_bot.py
```

The supervisor is only for Render, where nothing else is running.

---

## Before testing

### 1. Create the Discord application

1. <https://discord.com/developers/applications> → **New Application**, name it
   (e.g. "M2 Items").
2. **Bot** tab → **Reset Token** → copy it. This is `ITEMS_DISCORD_TOKEN`.
   Treat it like a password; anyone holding it controls the bot.
3. Still on the Bot tab, scroll to **Privileged Gateway Intents** and turn on
   **MESSAGE CONTENT INTENT**. Save.

   Without this the bot connects, looks online, and silently ignores every
   `!request` — it cannot see message text at all. This is the single most
   common reason a new bot "does nothing".

4. **OAuth2 → URL Generator**:
   - Scopes: **`bot`**
   - Bot permissions: **View Channels**, **Send Messages**, **Embed Links**,
     **Read Message History**, **Manage Messages**

   **Manage Messages** is what lets the bot pin its state message. Without it
   the bot still works, but its queue is easier to lose.

5. Open the generated URL, invite the bot to your server.
6. Create a **private officer channel** if you don't have one, and make sure
   the bot can see it. Whoever can see this channel can approve items — that
   *is* the permission model, so check its member list.

### 2. Get the service account JSON

You already have one in Render (the attendance bot uses it). Render dashboard →
your service → **Environment** → copy the whole value of
`GOOGLE_SERVICE_ACCOUNT_JSON`.

Inside that JSON is a `"client_email"` field, something like
`something@your-project.iam.gserviceaccount.com`. You need it in the next step.

### 3. Make a TEST COPY of the sheet

**Do not test against the real Logs Tracker.** Testing writes real ticks and
real counts, and undoing them by hand is exactly the sort of quiet damage this
bot exists to prevent.

1. Open Logs Tracker → **File → Make a copy**. Name it "Logs Tracker TEST".
2. **Share** the copy with the `client_email` from step 2, as **Editor**.
3. Copy the new sheet's ID from its URL — the long string between `/d/` and
   `/edit`.

### 4. Fill in `.env`

Add these three lines to `/Users/keithjustinnario/Documents/FB TIMER M2/.env`:

```
ITEMS_DISCORD_TOKEN=<the token from step 1>
ITEMS_SHEET_ID=<the TEST copy's id from step 3>
GOOGLE_SERVICE_ACCOUNT_JSON=<the whole JSON from step 2, on ONE line>
```

The JSON must be a single line. `.env` is already in `.gitignore`, so none of
this is committed.

### 5. Run preflight

```bash
.venv/bin/python items_preflight.py
```

This opens no Discord connection and writes nothing. It checks the service
account can open the sheet, the tabs exist, no two roster rows mean the same
player, and no item appears in both tabs — then parses a sample request built
from your own data. Fix anything it reports before going further.

### 6. Confirm the checkbox actually ticks

This is the one thing the 350 automated tests cannot prove: whether gspread
writes a real Sheets **checkbox** or the text `TRUE`.

```bash
.venv/bin/python items_preflight.py --write-test "<a player>" "<a special item>"
```

Use a real player name and special-log item from your sheet. Then **look at the
cell it names**:

- A **ticked checkbox** → correct, carry on.
- The **text `TRUE`** beside an empty box → wrong. Tell me and I'll fix the
  write mode; it's a small change.

The script refuses to run this against the real Logs Tracker.

---

## Testing

Start the bot:

```bash
.venv/bin/python -u items_bot.py
```

You should see `[items] logged in as ...`. Leave it running; `Ctrl-C` stops it.

Then, in Discord:

| # | Do this | Expect |
|---|---|---|
| 1 | `!setofficerchannel` **in the private officer channel** (you need admin) | Confirmation, plus a pinned `ITEMS_STATE_V1` message. Don't delete it — it *is* the queue. A busy queue adds more of them. |
| 2 | `!request <special item> <IGN>` from any channel | "Request queued", echoing the item and IGN it understood |
| 3 | The same request again | Refused as already pending |
| 4 | `!request <item> Kobeee` (a wrong name) | Refused, suggesting the closest real names |
| 5 | `!myrequests` | Lists your pending request |
| 6 | `!distribute` in the officer channel | Panel listing requests with a dropdown + ✅/❌ |
| 7 | `!distribute` in **any other** channel | Nothing at all — this is the security model working |
| 8 | Pick the request, click **✅** | Checkbox ticks, a `Distribution Log` row appears, panel updates |
| 9 | Request that **same special item** again | Refused — already has it |
| 10 | Four gear requests for one player, approve three | The 4th is refused at the daily cap |
| 11 | Click **❌** on something | Cleared from the queue, **nothing** written to the sheet |
| 12 | `!cancelrequest` as the requester | Withdraws your own pending request |
| 13 | `Ctrl-C`, restart the bot, `!distribute` | The pending queue is still there |

Test 13 is the one people skip. It's what proves a Render redeploy won't quietly
eat everyone's requests.

Check the `Distribution Log` tab afterwards: one row per approval, seven columns,
timestamps in Manila time.

---

## After testing — deploying to Render

### 1. Point back at the real sheet

In `.env`, change `ITEMS_SHEET_ID` back to the real Logs Tracker:

```
ITEMS_SHEET_ID=1Xx44UKBx0v5Pa0xbBzuVElEFZK-mdeQ5jHBBzBsKQgc
```

Delete the "Logs Tracker TEST" copy when you're done with it.

### 2. Merge the branch

```bash
git checkout main
git merge item-distribution-bot
git push
```

Or open a PR from `item-distribution-bot`, matching how you merged the last two.

### 3. Set the Render environment variables

Render dashboard → your `fb-timer-bot` service → **Environment**. Add:

| Key | Value | Notes |
|---|---|---|
| `ITEMS_DISCORD_TOKEN` | the bot token | Same one you tested with |
| `ITEMS_SHEET_ID` | `1Xx44UKBx0v5Pa0xbBzuVElEFZK-mdeQ5jHBBzBsKQgc` | The **real** sheet |
| `ITEMS_GEAR_DAILY_CAP` | `3` | Optional; defaults to 3 anyway |

`GOOGLE_SERVICE_ACCOUNT_JSON` is already there — the item bot reuses it. The
same service account must be an **Editor** on the real Logs Tracker; share it if
you haven't.

`render.yaml` already declares these keys, but Render will not invent values for
`sync: false` entries — you must type them into the dashboard.

### 4. Deploy and check

Render redeploys on push. In the service **Logs**, look for all three:

```
[supervisor] starting timer: ... bot.py
[supervisor] starting attendance: ... attendance_bot.py
[supervisor] starting items: ... items_bot.py
```

If you see `[items] not configured, missing: ...`, a variable is absent. The
supervisor deliberately leaves it stopped rather than restarting it in a loop —
the timer and attendance bots keep running normally either way.

### 5. Run `!setofficerchannel` once, in production

Local state does not carry over. Run it in the real officer channel, and confirm
the pinned message appears.

### 6. Tell your members

```
!request <item name> <IGN>     e.g.  !request Asta's Heart Kobe
!myrequests                    what you have pending
!cancelrequest                 withdraw a request
!itemhelp                      the rules
```

Rules: special logs once ever; gear logs 3 per day, resetting midnight Manila
time. Their IGN must match their row in the sheet.

---

## Notes

**The Gear Logs tab can stay unfinished.** Headers are read live on every
request, so columns you add work the moment you save them — no redeploy. Until a
gear column exists, gear requests for it are refused by name and special logs are
unaffected.

**Officer permissions are channel permissions.** To add or remove an officer,
change who can see the private channel. There is no role to configure.

**Never delete a pinned `ITEMS_STATE_V1` message.** They hold the pending queue.
The bot writes as many as the queue needs — one is normal, a busy day may show
several — and removes the extras itself as officers work the queue down. If one
is lost the bot says so on restart and recovers the rest; the requests that were
in the missing message are gone and must be re-requested. Approved history is
always safe in the sheet.

Requests are never silently discarded. If the queue ever grows past what the bot
can store (around 100 pending), the next member is told the queue is full and
asked to try again once officers have worked it down.

**If the bot looks online but ignores commands**, it is almost always the
Message Content Intent (step 1.3).

**Two known limitations**, both deliberate:

- A gear cell containing a *formula* would be replaced by a plain number.
  `attendance_sheet.py` already behaves this way against your production sheet,
  so this is consistent rather than new. Keep gear cells as plain counts.
- Two byte-identical duplicate rows in column A are refused at approval time
  rather than at request time. Preflight catches these before you ever hit it.
