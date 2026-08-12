# `!cancelpoll <item>` — design

## Problem

There is no way to undo a special-log raffle poll once `!poll` has posted it.
Deleting the poll message by hand leaves the `Raffle` record in the pinned
state. That record is undrawn, so `raffle_to_evict` (`items_state.py:481`)
refuses to evict it — it holds one of the 25 slots — and `!poll` for the same
item is refused while `ends_at` is in the future (`items_bot.py:1372`). The
officer must wait out the full poll duration before the item is pollable
again.

A raffle that has *closed* without a winner is already recoverable: `!poll`
supersedes it (`items_bot.py:1387`). Only the still-open case is stuck, and
that is exactly the gap this command fills.

## Command

`!cancelpoll <special log name>` — ends the poll, deletes the message, and
drops the raffle from state.

Item resolution goes through `items_state.find_raffle`, which matches on
`items_rules.normalize`. That is the same strictness as `resolve_special`
(exact match on the normalized name), so **no Google Sheets read is needed**
and cancelling cannot fail because Sheets is down.

## Access

`raffle_access(ctx)` + `_refuse_raffle`, identical to `!poll`. Add
`"cancelpoll"` to `_RAFFLE_COMMANDS` (`items_bot.py:459`) so the channel guard
confines it to the raffle channel. `test_every_registered_command_is_classified`
fails if that is missed.

## Placement

In `items_bot.py`, after `winner_cmd` and before the "no `@bot.command` below
this point" marker at `items_bot.py:1887`.

## Flow

All of it inside `async with _SHEET_LOCK` — the lock guards `_STATE`, not just
the sheet.

1. Blank argument → error embed with usage.
2. `raffle = items_state.find_raffle(_STATE, item_query)`. `None` → error
   embed naming the tracked raffles via `items_state.raffle_item_names`.
3. `raffle.winner` set → refuse. A drawn raffle is distribution history.
4. `raffle.ends_at <= items_rules.format_timestamp(items_rules.now_pht())` →
   refuse, pointing the officer at `!poll <item>`, which supersedes a closed
   undrawn raffle already.
5. Fetch the poll message from `bot.get_channel(raffle.channel_id) or
   ctx.channel` — the raffle's own channel, matching `list_cmd`
   (`items_bot.py:1633`), so a mid-poll channel move still resolves.
   - `discord.NotFound` → the message is already gone. Skip to step 7 and say
     so in the reply.
   - Any other exception → abort with an error embed, state untouched.
6. `await message.end_poll()`, then `await message.delete()`.
   - `end_poll` raising because Discord already ended the poll → treat as
     success and continue.
   - `end_poll` failing for any other reason → **abort, state untouched.** The
     poll is still live and still tracked, which is a consistent state.
   - `delete` failing → continue to step 7, and warn in the reply.
7. `_STATE.raffles.remove(raffle)`, then `await save_state(channel)` on the
   officer channel when it is set — same shape as `cancelrequest_cmd`
   (`items_bot.py:1200`). No `refresh_board()`: the board renders requests,
   not raffles.
8. Reply. `ok_embed` on the clean path; a warning embed when the delete failed
   or the message was already missing, telling the officer what to do by hand.

## State module

Unchanged. A plain `_STATE.raffles.remove(raffle)` on a raffle we hold is what
`poll_cmd` already does when superseding (`items_bot.py:1387`). Eviction and
`fits` are unaffected because state only shrinks.

## Tests

In `tests/test_items_bot.py`, using the existing `_configured_raffle`,
`_open_raffle`, `_raffle_ctx` and `FakeMessage` fakes. `FakeMessage` needs an
`end_poll` coroutine plus a `raise_on_end_poll` flag; it already has `delete`
and `raise_on_delete`, and `FakeChannel.fetch_message` already raises NotFound
for a deleted message.

- cancels an open poll: message ended and deleted, record gone from state
- refuses a drawn raffle, state intact
- refuses a closed-but-undrawn raffle, state intact
- refuses an unknown item name
- refuses a blank argument
- message already deleted → record dropped, reply says so
- `delete` raises → record dropped, reply warns
- `end_poll` raises → aborts, raffle still in state, message not deleted
- channel classification: `cancelpoll` resolves to the raffle channel

## Docs

Add the command to the `!itemhelp` body (`items_bot.py:1252`) and anywhere
`!poll` is documented in `README.md` and `docs/item-bot-setup.md`.
