# Item Distribution Bot — Design

Date: 2026-08-07
Status: Approved design, ready for implementation planning

## Problem

Guild members ask officers for items ad hoc, in whatever channel they happen to
be in. Nobody tracks who has already received what, so a few members collect far
more than their share and others get nothing. The officers have no record to
check against at the moment they hand an item over.

## Solution

A third Discord bot. Members request items with a command; the requests queue
up; officers review the whole queue at once and approve or deny each one with
buttons. Every approval is written to the Logs Tracker spreadsheet, and the bot
refuses requests that would break the guild's allocation rules before an officer
ever sees them.

## Scope

In scope: request queue, officer approval panel, sheet writes, allocation rule
enforcement, audit trail.

Out of scope: item availability/inventory tracking (the bot never knows how many
of an item the guild actually holds), automatic distribution without an officer,
any change to the timer or attendance bots.

## Constraints

- **Must not endanger the two production bots.** `bot.py` (timer) and
  `attendance_bot.py` are live and in daily use. The new bot runs as its own OS
  process with its own Discord token and its own spreadsheet.
- **Render free tier.** One web service, 512MB, 0.1 CPU, ephemeral disk, shared
  750 instance-hours. A second Render service is not an option — see the module
  docstring in `supervisor.py`.
- **Timezone is Asia/Manila** (`BOT_TZ`), matching the other bots.

## Data model

Spreadsheet: **Logs Tracker**, ID `1Xx44UKBx0v5Pa0xbBzuVElEFZK-mdeQ5jHBBzBsKQgc`.
Separate from the attendance spreadsheet (`SHEET_ID`); referenced by a new env
var `ITEMS_SHEET_ID`.

Both existing tabs share the attendance sheet's shape: row 1 is a header row of
item names, column A is `Player Name`, and the intersection cell is that
player's record for that item.

Each tab carries its own column A. IGN resolution uses `Special Logs` column A as
the canonical roster, but the write itself locates the row within the tab being
written. A player present in `Special Logs` but missing from `Gear Logs` causes
that gear write to be refused by name — the bot never adds a row to reconcile
the two.

| Tab | Exists? | Cell type | On approval |
|---|---|---|---|
| `Special Logs` | yes | checkbox (`TRUE`/`FALSE`) | set to `TRUE` |
| `Gear Logs` | user will create | integer count | increment by 1 (blank = 0) |
| `Distribution Log` | **created by the bot** | append-only rows | append one row |

`Distribution Log` columns, in order:

```
Timestamp (PHT) | IGN | Item | Type | Officer | Discord User ID | Request ID
```

Created via the existing `attendance_sheet.get_or_create_tab(spreadsheet, title,
header)`.

### Why the Distribution Log tab is required

The `Gear Logs` cell holds a lifetime total, not a dated record. It cannot
answer "how many gear logs has this player received *today*". The
`Distribution Log` is the only source of truth for the daily cap, and doubles as
the audit trail the guild currently lacks.

Denied requests are **not** written to any tab. A denial removes the request from
the queue and nothing else.

## Allocation rules

| Item type | Rule | Evaluated against |
|---|---|---|
| Special log | once per player, ever | the `Special Logs` checkbox itself |
| Gear log | 3 per player per day, any mix of items | count of `Distribution Log` rows for that IGN whose timestamp falls on today's PHT date |

"Day" boundary is 00:00 Asia/Manila.

### Item type is derived, not configured

The bot resolves an item name against `Special Logs` headers, then `Gear Logs`
headers.

- Found in exactly one tab → that is its type.
- Found in neither → refuse, listing near-miss header names.
- Found in both → refuse, naming the conflict. The bot does not guess which tab
  the officer meant.

This follows the refuse-rather-than-guess convention established in
`attendance_roster.py` and `attendance_sheet.find_column`.

### The cap is enforced twice

1. **At `!request`** — immediate feedback to the member, and it keeps ineligible
   requests out of the officers' panel entirely.
2. **At the moment an officer clicks ✓**, inside the write lock — re-read from
   the sheet, then write.

Both checks are required. Without the second, a member queues five requests
before any is approved and every one of them passes the first check.

**Pending requests count toward the cap at request time.** Otherwise a member
stacks five pending gear requests and officers approve them one at a time,
each individually appearing to be within the cap.

## Commands

### `!request <item name> <IGN>` — any member, any channel

Parsing: the **last whitespace-separated token is the IGN**; everything before it
is the item name. Both parts are free text, so the bot echoes back its
interpretation ("Requesting **Asta's Heart** for **Kobe**") and a mis-parse is
visible immediately.

Resolution, both using `attendance_roster.match_names`' fuzzy matcher:

- IGN against column A of `Special Logs`.
- Item against the header rows of both item tabs.

Unknown or ambiguous input is refused with the near-misses named. The bot never
creates a player row or a column.

**IGN memory.** The bot records `discord_user_id -> last IGN used` in its state.
A later request with a different IGN prompts for confirmation ("You used `Kobe`
before — is `Kobee` correct?"). This is the guard against the typo risk inherent
in members typing their own IGN.

Rejections at request time:

- IGN not found / ambiguous
- Item not found / ambiguous / present in both tabs
- Special log the player already has (checkbox is `TRUE`)
- Gear log when approvals-today + pending-gear-requests already ≥ 3
- A duplicate of a request the member already has pending

### `!distribute` — officer channel only

Posts one panel message. The embed lists every pending request, numbered, one
line each, in the visual style of the existing `!bosses` output:

```
📦 Pending Item Requests

1. Kobe — Asta's Heart          [Special]  ⚠️ already has it
2. Dajz — Benji's Blood         [Gear]     2/3 today
3. Smth — Amentis' Foot         [Special]  ✅ eligible
```

Components: a select menu listing the pending requests, plus **✓ Approve** and
**✗ Deny** buttons that act on the current selection. Discord caps a select menu
at 25 options; beyond that the panel paginates.

- **✓** writes the item cell and appends the `Distribution Log` row, then removes
  the request from the queue.
- **✗** removes the request from the queue. No sheet write of any kind.
- The panel edits in place; resolved requests disappear from it.
- The panel expires after 15 minutes. The queue is unaffected — officers re-run
  `!distribute`.

### `!cancelrequest [item name]` — the requester

Removes the caller's own pending request. With no argument and exactly one
pending request, cancels it; with several pending, the bot lists them and asks
which.

### `!setofficerchannel` — administrator, run inside the private channel

Records that channel as the officer channel. `!distribute` is accepted only
there, and the bot's state message is pinned there.

### `!myrequests`, `!itemhelp` — read-only helpers

## Authorization

**The private channel is the gate.** There is no role configuration.

- `!distribute` is accepted only in the recorded officer channel.
- Button and select interactions attach to a message in that channel, so only
  members Discord already permits to see the channel can interact with it. The
  handler additionally verifies the interaction's channel ID matches the
  recorded officer channel, so a panel that somehow ends up elsewhere is inert.
- `!setofficerchannel` requires the Discord `administrator` permission, matching
  `attendance_bot.setofficerrole`.

## State and persistence

The pending queue and the `discord_user_id -> IGN` memory live as JSON in a
bot-authored, pinned message in the officer channel, prefixed `ITEMS_STATE_V1`.
This mirrors `bot.py`'s `FBTIMER_STATE_V1` mechanism, which is proven in
production on this same Render instance.

- **Why not in-memory:** Render's free tier restarts on every deploy and can spin
  down. An in-memory queue silently loses pending requests.
- **Why not a sheet tab:** every `!request` would become a Sheets write, adding
  latency and quota pressure to the member-facing path.
- **Bootstrap:** before `!setofficerchannel` has run, the bot has nowhere to
  persist and refuses `!request` with an explanatory message.
- **2000-char limit:** at >1990 characters the encoder drops the oldest pending
  requests and the bot posts a visible warning naming what was dropped. It never
  truncates silently.
- **Restart recovery:** on ready, the bot reads its pinned state message from the
  officer channel.

Each request carries a `Request ID` (short random token), which is written to the
ledger and used to detect two officers acting on the same request — the second
gets an ephemeral "already handled by @X".

## Concurrency

A module-level `asyncio.Lock` serializes every read-then-write sequence, and all
blocking `gspread` calls run via `asyncio.to_thread`, matching
`attendance_bot._SHEET_LOCK` and `_locked()`. The read of current state and the
write that depends on it happen inside the same lock acquisition; otherwise two
simultaneous approvals both read "2 today" and both write, yielding 4.

## Error handling

| Failure | Behaviour |
|---|---|
| Sheet unreachable on ✓ | Report the failure; **request stays queued**. Nothing is lost. |
| Sheet unreachable on `!request` | Refuse with "sheet unreachable", do not queue. |
| `Gear Logs` tab absent | `!request` for a gear item refuses with a clear message naming the missing tab. Special logs keep working. |
| Player row missing | Refuse. The bot never adds a row. |
| Item column missing | Refuse. The bot never adds a column. |
| Non-numeric value in a gear cell | Refuse that write and name the cell, rather than overwriting a value it does not understand. |
| Credentials missing at startup | Print to stderr, `sys.exit(78)`. The supervisor leaves it stopped; the other two bots are unaffected. |
| Panel expired | Buttons report expiry and direct the officer to re-run `!distribute`. |

## Modules

| File | Responsibility | Depends on |
|---|---|---|
| `items_rules.py` | Pure logic: PHT day boundary, cap arithmetic, item-type resolution, command parsing. No I/O. | stdlib only |
| `items_sheet.py` | Logs Tracker access: read headers/players, read a cell, set checkbox, increment count, append ledger row, count today's approvals. | `gspread`, `attendance_sheet` helpers |
| `items_state.py` | Encode/decode the pinned state message; queue operations. | stdlib only |
| `items_bot.py` | Discord wiring: commands, panel embed, button/select handlers. | `discord.py`, the three above |

`items_rules.py` and `items_state.py` are pure and fully testable without
network or Discord. `items_sheet.py` is tested against the existing fakes.
This split keeps each file small enough to reason about whole.

Reused from the existing codebase, not reimplemented:

- `attendance_roster.normalize`, `match_names`, `ALIASES` — IGN matching
- `attendance_sheet.open_spreadsheet`, `read_headers`, `read_players`,
  `find_column`, `get_or_create_tab`, `SheetStructureError`
- `attendance_bosses.header_base` — strips a `" - N"` suffix from headers

## Changes to existing files

Additive only. Neither production bot's logic is modified.

- `supervisor.py`: append
  `ChildSpec("items", [sys.executable, "-u", "items_bot.py"])` to `CHILDREN`.
  It uses the default `NO_RESTART_CODES`, so exit 78 leaves it stopped rather
  than crash-looping against the timer.
- `render.yaml`: add `ITEMS_DISCORD_TOKEN` and `ITEMS_SHEET_ID`
  (both `sync: false`).
- `README.md`: document the new bot.

## Environment variables

| Name | Required | Purpose |
|---|---|---|
| `ITEMS_DISCORD_TOKEN` | yes | The third bot's Discord token |
| `ITEMS_SHEET_ID` | yes | Logs Tracker spreadsheet ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | Reused; the same service account must be shared on Logs Tracker as an Editor |
| `BOT_TZ` | no | Already set to `Asia/Manila` |
| `ITEMS_GEAR_DAILY_CAP` | no | Defaults to 3 |

## Discord bot setup (user action)

1. Discord Developer Portal → New Application → Bot → Reset Token, copy it.
2. Enable **Message Content Intent** under Privileged Gateway Intents. Without
   it, `!request` is never seen.
3. OAuth2 URL Generator → scopes `bot` → permissions: View Channels, Send
   Messages, Embed Links, Read Message History, Manage Messages (to pin the
   state message). Invite to the guild.
4. Share the Logs Tracker spreadsheet with the service account email as
   **Editor**.
5. Add `ITEMS_DISCORD_TOKEN` and `ITEMS_SHEET_ID` in Render.
6. Run `!setofficerchannel` in the private officer channel.

## Risks

- **Memory on a 512MB instance.** A third `discord.py` process is the main
  deployment risk. Measured RSS of all three processes is a gate before merge.
  Fallback if it does not fit: run the item commands inside the attendance
  process — same modules, but shared fate with the attendance bot.
- **Members typing their own IGN.** Mitigated by matching against column A and
  by IGN memory, but not eliminated. If it proves annoying, the natural upgrade
  is to store `discord_user_id -> IGN` permanently after the first confirmed
  request.

## Testing

Following the existing pytest layout under `tests/`, with the shared
`FakeWorksheet` / `FakeSpreadsheet` fakes in `tests/conftest.py` and
locally-defined Discord fakes as in `test_attendance_bot.py`. No test starts a
Discord client or touches the network.

Coverage that matters:

- PHT day boundary: an approval at 23:59 and one at 00:01 fall on different days.
- Cap arithmetic including pending requests.
- Second cap check rejects an approval that the first check allowed.
- Item found in both tabs → refused.
- Special log already `TRUE` → refused.
- Gear increment from blank, from `0`, from `2`; non-numeric → refused.
- Deny writes nothing to any tab.
- State encode/decode round-trip; oversize state drops oldest and warns.
- Two officers approving the same Request ID → exactly one write.
- `!distribute` outside the officer channel is ignored.

## Success criteria

1. A member requesting a special log they already hold is refused without an
   officer being involved.
2. A member requesting a 4th gear log in one PHT day is refused; the same
   request after midnight succeeds.
3. Every ✓ produces exactly one `Distribution Log` row and one cell update.
4. Every ✗ produces no sheet change and clears the request.
5. A Render redeploy mid-queue loses no pending request.
6. The timer and attendance bots run unchanged throughout.
