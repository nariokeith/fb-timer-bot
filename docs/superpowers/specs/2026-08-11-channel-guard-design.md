# Per-bot channel guard

**Date:** 2026-08-11
**Status:** approved, not yet implemented

## Problem

All three bots share the `!` prefix in one guild. discord.py dispatches a
command from any channel the bot can read, so every command works in every
channel. Typing `!request` in the attendance channel runs the items bot's
request flow — a real sheet write from a channel that has nothing to do with
items.

Only two guards exist today:

- `!distribute` checks `is_officer_channel` (`items_bot.py:1068`)
- `!poll` / `!list` / `!winner` check `raffle_access` (`items_bot.py:411`)

Everything else is open: `!request`, `!cancelrequest`, `!myrequests`,
`!itemhelp`, `!killed`, `!boss`, `!bosses`, `!timer`, `!attendance`,
`!undoattendance`, `!setweek`, `!setofficerrole`, `!attendancehelp`,
`!setraffleroles`.

The `!set*channel` commands only record where a bot *posts*. They have never
constrained where it *listens*.

## Goal

A command runs only in the channel(s) that bot is configured for. Anywhere
else it is silently ignored.

## Non-goals

- No Discord server changes. No channel is created, deleted, renamed, moved,
  or re-permissioned.
- No change to what any existing command does — same arguments, same output,
  same sheet writes. Only *where* it is accepted changes.
- Not fixing the pre-existing failure
  `test_successful_requests_repost_the_board_on_every_fifth_request`
  (board repost counter, from commit `9160e2a`). Out of scope; recorded here
  so it is not mistaken for a regression from this work.

## Decisions

| Question | Decision |
| --- | --- |
| Wrong channel | Silent — no message, no reaction. Matches `raffle_access`. |
| DMs | Blocked once a channel is configured. |
| Unconfigured bot | Guard inert — allow everywhere. Preserves today's behavior exactly. |
| Exempt commands | Only the `set*channel` commands. |
| Items member commands | Queue channel only. |
| Timer commands | Notification channel + TOD-log channel. Storage channel blocked. |
| `!setraffleroles` | Officer channel. |

**Resolved conflict:** "block DMs" and "unconfigured → allow everywhere"
disagree in one corner. Resolved toward preserving today's behavior: while a
bot is unconfigured the guard is fully inert, DMs included. Once a channel is
set, a DM channel ID never matches a guild channel ID, so DMs are refused.

## Architecture

### New module: `channel_guard.py`

The whole rule in one testable place, with no Discord dependency in the core
predicate.

```python
class WrongChannel(commands.CheckFailure):
    """Raised when a command is typed outside its configured channel."""


def allows(channel_id, allowed) -> bool:
    """True when a command may run in `channel_id`.

    `allowed` may contain None entries (unset settings); they are dropped.
    An empty result means nothing is configured, so the guard is inert.
    """
    configured = {cid for cid in allowed if cid is not None}
    if not configured:
        return True
    return channel_id in configured
```

Plus `make_check(resolver)`, which turns a per-bot function
`ctx -> allowed-channels | EXEMPT` into a `@bot.check` that raises
`WrongChannel` on refusal.

Two properties carry the safety of this design:

1. **`WrongChannel` subclasses `CheckFailure` but is distinct from it.**
   `attendance_bot.py:904` and `items_bot.py:1261` both *reply* to any
   `CheckFailure`. Without a distinct type, every stray command would post
   "Couldn't Run That" into the wrong channel — noisier than the bug being
   fixed. Each handler swallows `WrongChannel` specifically and leaves all
   other `CheckFailure` handling untouched.

2. **Empty config allows.** A bot that has not been set up, or whose config
   fails to load, degrades to today's behavior rather than going dead.

### `bot.py` (field boss timer)

`#fieldboss-tod-log` is where `!killed` is typed, and its ID has never been
stored. A guard cannot restrict to a channel it cannot name, so one new
setting is required.

- `data["tod_channel_id"]`, default `None`. Added to `load_local`,
  `encode_state`, and `decode_state`. Read with `.get()` in `decode_state`
  so **pinned state messages written before this change still decode**.
- New command `!settodchannel` — records the current channel's ID. No `clear`
  counterpart (dropped as unnecessary; re-running `!settodchannel` elsewhere
  moves it).
- **No permission decorator**, matching this bot's existing setup commands:
  `!setchannel` (`bot.py:549`), `!setstoragechannel` (`:570`) and
  `!clearstoragechannel` (`:590`) are all ungated today. Adding a gate only
  to the new command would be inconsistent, and gating the existing three is
  a behavior change this work has been told not to make. Worth noting that
  any member can therefore move the timer bot's channels — a pre-existing
  property, not one introduced here.
- **Exempt:** `!setchannel`, `!setstoragechannel`, `!clearstoragechannel`,
  `!settodchannel`
- **Guarded to `{channel_id, tod_channel_id}`:** `!killed`, `!boss`,
  `!bosses`, `!timer`

`#bot-storage` is blocked, per decision. It stays purely the pinned-state
store.

`bot.py` has no `on_command_error`, so a refused command would print an
"Ignoring exception in command" traceback to Render's logs. A minimal handler
is added that swallows `WrongChannel` and `CommandNotFound` — the latter
already spams the logs today, since all three bots share the `!` prefix — and
prints anything else to stderr. This is the one behavior change outside the
guard itself, and it only affects log output.

### `attendance_bot.py`

This bot stores no channel ID at all; its config lives in the sheet's Config
tab (`target_tab`, `officer_role_ids`). One new key follows that pattern.

- Config key `attendance_channel_id`, written with the existing
  `write_config`. No new storage layer.
- New command `!setattendancechannel`, gated with
  `@commands.has_permissions(administrator=True)` — matching
  `!setofficerrole` (`attendance_bot.py:1358`) and items_bot's set-channel
  commands. (`!setweek` at `:1323` is ungated, but it selects a sheet tab,
  not who may command the bot.)
- The value is **cached in a module global**, loaded in `on_ready` and
  refreshed when set. The guard must not cost a Sheets API call per command;
  `!attendancehelp` performs zero sheet reads today and must stay that way.
  Trade-off: editing that cell by hand needs a bot restart, which the
  command's reply states.
- If the `on_ready` read fails, the cache stays `None` and the guard is
  inert — today's behavior, not a dead bot.
- **Exempt:** `!setattendancechannel`
- **Guarded:** `!attendance`, `!undoattendance`, `!setweek`,
  `!setofficerrole`, `!attendancehelp`

### `items_bot.py`

No new state. The three channel IDs already exist in `State`.

| Commands | Allowed channel |
| --- | --- |
| `!setofficerchannel`, `!setqueuechannel`, `!setrafflechannel` | exempt |
| `!request`, `!cancelrequest`, `!myrequests`, `!itemhelp` | queue |
| `!distribute`, `!setraffleroles` | officer |
| `!poll`, `!list`, `!winner` | raffle |

The existing inner guards stay **untouched**. `is_officer_channel` in
`!distribute` and `raffle_access` in the raffle commands are now redundant on
the happy path, but they still produce the "no raffle channel is set" message
for administrators — which a silent global guard cannot. Removing them would
lose that affordance.

The officer-panel button check at `items_bot.py:969` is unaffected; it guards
an interaction, not a command.

## Migration

Deploy is safe with no action taken: every bot's guard is inert until
configured, and `items_bot` only starts guarding a command once the relevant
channel has been set (which, for the officer channel, is already true).

Two one-time commands complete the setup:

- `!settodchannel` in `#fieldboss-tod-log`
- `!setattendancechannel` in the attendance channel

`items_bot` needs nothing.

## Verification

- `tests/test_channel_guard.py` — `allows`: unconfigured, match, no match,
  DM, `None` entries mixed with real IDs.
- Per-bot wiring tests: an exempt command passes in a foreign channel; a
  guarded command is refused in a foreign channel; a guarded command passes
  in its own channel; an unconfigured bot allows everywhere.
- `WrongChannel` is swallowed by each error handler, and a non-`WrongChannel`
  `CheckFailure` still produces its existing reply. This is the regression
  most likely to be missed, so it gets an explicit test.
- Timer state round-trip: `tod_channel_id` survives
  `encode_state`/`decode_state`, and a pre-change state message lacking the
  key still decodes. There is no `tests/test_bot.py` today; this adds one.
- Full `pytest` run compared against the recorded baseline of
  **523 passed, 1 failed** (the pre-existing board-repost failure above).
