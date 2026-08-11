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
  same sheet writes. Only *where* it is accepted changes. **One exception,
  requested explicitly:** `!setweek` gains an administrator gate (see below).
## Pre-existing test failure (in scope)

`test_successful_requests_repost_the_board_on_every_fifth_request` fails at
baseline: it asserts 4 board edits and gets 2.

Commit `9160e2a "change the board repost count"` lowered
`BOARD_REPOST_EVERY` from 5 to 3 (`items_bot.py:43`) and did not update this
test, which hardcodes the old cadence. Production is correct; the test is
stale. The neighbouring
`test_a_refused_request_does_not_advance_the_board_repost_cadence` survived
because it derives its numbers from `BOARD_REPOST_EVERY - 2`.

Fix: rewrite the stale test in terms of `BOARD_REPOST_EVERY` rather than
literals, so it cannot drift again, and rename it away from "fifth". No
production change.

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
  `#fieldboss-tod-log` is a private, officer-only channel.
- New command `!settodchannel` — records the current channel's ID. No `clear`
  counterpart (dropped as unnecessary; re-running `!settodchannel` elsewhere
  moves it).
- Gated with `@commands.has_permissions(administrator=True)`. This bot's
  existing setup commands are all ungated (`!setchannel` `bot.py:549`,
  `!setstoragechannel` `:570`, `!clearstoragechannel` `:590`), but gating a
  brand-new command changes no existing behavior, and an exempt command that
  redirects where the bot listens is exactly the one that should not be
  member-runnable. Left unfixed and noted: the three existing ungated setup
  commands mean a member can still repoint the timer bot's notification and
  storage channels. That is pre-existing, out of scope here, and worth a
  follow-up.
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
  commands.
- `!setweek` (`attendance_bot.py:1323`) gains the same administrator gate.
  It is currently ungated at the decorator level, though it does call
  `_require_officer` internally, so today any configured officer can
  repoint attendance at a different sheet tab. The gate narrows that to
  administrators. **This is a deliberate behavior change to an existing
  command, requested explicitly** — the only one in this work. Its existing
  `_require_officer` check stays, so both must now pass.
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
- `!setweek` rejects a non-administrator and still honours
  `_require_officer` for an administrator.
- Full `pytest` run against the recorded baseline of **523 passed, 1 failed**.
  Target: 0 failed, with the board-repost test fixed and new tests added.
