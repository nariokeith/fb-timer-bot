# Per-Bot Channel Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each bot accept commands only in the channel(s) it is configured for, silently ignoring them everywhere else.

**Architecture:** One new module, `channel_guard.py`, holds a pure predicate (`allows`), a distinct exception (`WrongChannel`), and a `make_check` factory. Each bot registers one global `bot.add_check(...)` built from a bot-specific resolver that maps a command name to the channel IDs it may run in. Two bots gain a new setting for a channel the code has never stored.

**Tech Stack:** Python 3.14, discord.py 2.7, pytest, gspread.

## Global Constraints

- **Unconfigured means inert.** When no relevant channel ID is set, the guard allows the command. A bot that has not been set up, or whose state failed to load, must behave exactly as it does today rather than going dead.
- **Silence on refusal.** No message, no reaction. Matches the existing `raffle_access` behavior at `items_bot.py:411`.
- **`WrongChannel` must subclass `commands.CheckFailure` but be swallowed separately.** `attendance_bot.py:904` and `items_bot.py:1261` reply to any `CheckFailure`. Every other `CheckFailure` must keep its current reply.
- **No Discord server changes.** No channel is created, deleted, renamed, moved, or re-permissioned.
- **Only one existing command changes behavior:** `!setweek` gains an administrator gate. Everything else keeps identical arguments, output, and sheet writes.
- **Never call `bot.save_local()` or `persist()` from a test.** `data.json` is a real tracked file holding live channel IDs. Tests monkeypatch `bot.data` instead.
- Run tests with `.venv/bin/python -m pytest` — there is no `python` or bare `pytest` on PATH.
- Baseline before this work: **523 passed, 1 failed**. Target on completion: **0 failed**.

## File Structure

- **Create `channel_guard.py`** — the entire rule. No bot-specific knowledge, no state. Imported by all three bots.
- **Create `tests/test_channel_guard.py`** — unit tests for `allows`, `make_check`, `WrongChannel`.
- **Create `tests/test_bot.py`** — first test file for the timer bot; covers `tod_channel_id` state round-trip and its resolver.
- **Modify `bot.py`** — `tod_channel_id` setting, `!settodchannel`, resolver, global check, new `on_command_error`.
- **Modify `attendance_bot.py`** — `attendance_channel_id` sheet key + cache, `!setattendancechannel`, resolver, global check, `WrongChannel` swallow, `!setweek` admin gate.
- **Modify `items_bot.py`** — resolver, global check, `WrongChannel` swallow.
- **Modify `tests/test_items_bot.py`** — fix the stale board-repost test; add resolver tests.
- **Modify `tests/test_attendance_bot.py`** — resolver, cache-loading, and `!setweek` gate tests.

---

### Task 1: The shared guard module

**Files:**
- Create: `channel_guard.py`
- Test: `tests/test_channel_guard.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `EXEMPT` (sentinel str), `WrongChannel(commands.CheckFailure)`, `allows(channel_id: int, allowed) -> bool`, `make_check(resolver) -> coroutine function`. `resolver(ctx)` returns `EXEMPT` or an iterable of `int | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_channel_guard.py`:

```python
"""The rule deciding whether a command may run in the channel it was typed in."""

import asyncio

import pytest
from discord.ext import commands

import channel_guard


class FakeCommand:
    def __init__(self, name):
        self.name = name
        self.qualified_name = name


class FakeCtx:
    def __init__(self, channel_id, command_name="somecmd"):
        self.channel = type("Channel", (), {"id": channel_id})()
        self.command = FakeCommand(command_name)


def test_unconfigured_allows_every_channel():
    # The whole safety property: a bot nobody has set up behaves as before.
    assert channel_guard.allows(123, (None, None)) is True
    assert channel_guard.allows(123, ()) is True


def test_matching_channel_is_allowed():
    assert channel_guard.allows(123, (123,)) is True


def test_other_channel_is_refused_once_configured():
    assert channel_guard.allows(999, (123,)) is False


def test_none_entries_are_ignored_but_real_ids_still_bind():
    # A bot with only some of its channels set still guards on the ones it has.
    assert channel_guard.allows(123, (None, 123)) is True
    assert channel_guard.allows(999, (None, 123)) is False


def test_exempt_allows_every_channel_even_when_configured():
    assert channel_guard.allows(999, channel_guard.EXEMPT) is True


def test_wrong_channel_is_a_check_failure_but_its_own_type():
    # Both bots' error handlers reply to CheckFailure. They must be able to
    # swallow this one specifically without silencing the others.
    assert issubclass(channel_guard.WrongChannel, commands.CheckFailure)
    assert channel_guard.WrongChannel is not commands.CheckFailure


def test_check_passes_in_an_allowed_channel():
    check = channel_guard.make_check(lambda ctx: (123,))
    assert asyncio.run(check(FakeCtx(123))) is True


def test_check_raises_wrong_channel_elsewhere():
    check = channel_guard.make_check(lambda ctx: (123,))
    with pytest.raises(channel_guard.WrongChannel):
        asyncio.run(check(FakeCtx(999)))


def test_check_ignores_a_message_that_is_not_a_command():
    # ctx.command is None for an unknown "!foo"; there is nothing to guard.
    ctx = FakeCtx(999)
    ctx.command = None
    check = channel_guard.make_check(lambda ctx: (123,))
    assert asyncio.run(check(ctx)) is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_channel_guard.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'channel_guard'`

- [ ] **Step 3: Write the implementation**

Create `channel_guard.py`:

```python
"""Where each bot is willing to accept commands.

All three bots share the "!" prefix in one guild, so discord.py hands a
command to whichever bot has it registered, from any channel that bot can
read -- !request typed in the attendance channel really did run the items
bot's request flow. The !set*channel commands only ever recorded where a
bot *posts*; they never constrained where it listens. Each bot registers
one global check built from this module to close that gap.
"""

from discord.ext import commands

# Returned by a resolver for the commands that must work anywhere. The
# !set*channel commands are how a channel gets configured in the first
# place, so they cannot themselves require one -- gating them would make
# a fresh guild unbootstrappable and a mistyped channel unfixable.
EXEMPT = "\x00exempt"


class WrongChannel(commands.CheckFailure):
    """A command was typed outside the channel(s) it is configured for.

    Deliberately its own subclass rather than a bare CheckFailure: both
    attendance_bot and items_bot reply to CheckFailure in their error
    handlers, so a bare one would post "couldn't run that" into every
    channel this guard exists to keep quiet -- noisier than the leak it
    fixes. Handlers swallow this type alone, leaving every other
    CheckFailure (bad input, missing role) replying exactly as before.
    """


def allows(channel_id, allowed) -> bool:
    """True when a command may run in `channel_id`.

    `allowed` is EXEMPT, or an iterable that may contain None for
    settings never configured; those are dropped. When nothing is left
    the bot is unconfigured and the guard stays inert -- so deploying
    this before anyone runs the setup commands changes no behavior, and
    a bot whose saved state fails to load degrades to today's behavior
    instead of refusing every command.
    """
    if allowed is EXEMPT:
        return True
    configured = {cid for cid in allowed if cid is not None}
    if not configured:
        return True
    return channel_id in configured


def make_check(resolver):
    """Build the global check a bot registers with bot.add_check().

    `resolver(ctx)` returns EXEMPT, or the channel ids ctx.command may
    run in. Refusal raises rather than returning False so the bot's
    error handler can tell this apart from a command that merely
    declined, and so discord.py stops before the command body runs.
    """

    async def check(ctx) -> bool:
        # None for an unrecognised "!foo" -- three bots share the prefix,
        # so most messages reaching any one bot are another bot's command.
        if ctx.command is None:
            return True
        if allows(ctx.channel.id, resolver(ctx)):
            return True
        raise WrongChannel(f"{ctx.command.qualified_name} is not used in this channel")

    return check
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_channel_guard.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add channel_guard.py tests/test_channel_guard.py
git commit -m "Add the shared channel-guard rule"
```

---

### Task 2: Fix the stale board-repost test

**Files:**
- Modify: `tests/test_items_bot.py:790-819`

**Interfaces:**
- Consumes: `items_bot.BOARD_REPOST_EVERY` (currently `3`, `items_bot.py:43`).
- Produces: nothing.

Independent of the guard; done first so the suite is green before new work lands and any later failure is unambiguously ours.

- [ ] **Step 1: Confirm the failure and its cause**

Run: `.venv/bin/python -m pytest "tests/test_items_bot.py::test_successful_requests_repost_the_board_on_every_fifth_request" -q`
Expected: FAIL, `assert 2 == 4`.

Cause: commit `9160e2a` lowered `BOARD_REPOST_EVERY` from 5 to 3 and left this test's hardcoded 4/5 behind. Production is correct. Do **not** change `items_bot.py`.

- [ ] **Step 2: Rewrite the test in terms of the constant**

Replace the whole of `test_successful_requests_repost_the_board_on_every_fifth_request` (`tests/test_items_bot.py:790-819`) with:

```python
def test_successful_requests_repost_the_board_on_every_nth_request(monkeypatch):
    # Derived from BOARD_REPOST_EVERY, never hardcoded: this test asserted a
    # cadence of 5 for a while after 9160e2a lowered the constant to 3.
    before_repost = items_bot.BOARD_REPOST_EVERY - 1
    state_channel, board_channel, first_board = _queue_board(monkeypatch)

    _queue_successes(monkeypatch, before_repost)

    assert first_board.edit_calls == before_repost
    assert board_channel.sent == []

    _queue_successes(monkeypatch, 1)

    second_board = board_channel.sent[0]
    saved = items_state.decode_state(state_channel.sent[0].content).state
    assert first_board.deleted
    assert second_board.pinned
    assert items_bot._STATE.board_message_id == second_board.id
    assert saved.board_message_id == second_board.id

    _queue_successes(monkeypatch, before_repost)

    assert second_board.edit_calls == before_repost
    assert len(board_channel.sent) == 1

    _queue_successes(monkeypatch, 1)

    third_board = board_channel.sent[1]
    saved = items_state.decode_state(state_channel.sent[0].content).state
    assert second_board.deleted
    assert third_board.pinned
    assert saved.board_message_id == third_board.id
```

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/python -m pytest -q 2>&1 | tail -3`
Expected: **0 failed**, and the passed count is 534 (523 baseline passes + 10 from Task 1 + the 1 now-fixed test). Record the exact number for later comparison.

- [ ] **Step 4: Commit**

```bash
git add tests/test_items_bot.py
git commit -m "Fix board-repost test left behind by 9160e2a"
```

---

### Task 3: Wire the guard into items_bot

**Files:**
- Modify: `items_bot.py` (imports near `:15-29`; new resolver after `raffle_access`; handler at `:1261`)
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `channel_guard.EXEMPT`, `channel_guard.make_check`, `channel_guard.WrongChannel`.
- Produces: `items_bot.command_channels(ctx)` returning `EXEMPT` or a tuple of `int | None`.

Done before the other two bots because it needs no new setting — its three channel IDs already exist on `items_state.State`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_bot.py`:

```python
class FakeGuardCtx:
    def __init__(self, channel_id, command_name):
        self.channel = FakeChannel(channel_id)
        self.command = type(
            "Cmd", (), {"name": command_name, "qualified_name": command_name}
        )()


def _configured_state():
    return items_state.State(
        officer_channel_id=10, queue_channel_id=20, raffle_channel_id=30
    )


def test_member_commands_are_confined_to_the_queue_channel(monkeypatch):
    monkeypatch.setattr(items_bot, "_STATE", _configured_state())
    for name in ("request", "cancelrequest", "myrequests", "itemhelp"):
        ctx = FakeGuardCtx(20, name)
        assert items_bot.command_channels(ctx) == (20,)
        # The reported bug: !request in the attendance channel.
        assert not channel_guard.allows(999, items_bot.command_channels(ctx))


def test_officer_and_raffle_commands_use_their_own_channels(monkeypatch):
    monkeypatch.setattr(items_bot, "_STATE", _configured_state())
    for name in ("distribute", "setraffleroles"):
        assert items_bot.command_channels(FakeGuardCtx(10, name)) == (10,)
    for name in ("poll", "list", "winner"):
        assert items_bot.command_channels(FakeGuardCtx(30, name)) == (30,)


def test_set_channel_commands_stay_usable_anywhere(monkeypatch):
    monkeypatch.setattr(items_bot, "_STATE", _configured_state())
    for name in ("setofficerchannel", "setqueuechannel", "setrafflechannel"):
        ctx = FakeGuardCtx(999, name)
        assert items_bot.command_channels(ctx) is channel_guard.EXEMPT


def test_requests_are_unrestricted_until_a_queue_channel_is_set(monkeypatch):
    # Deploying before anyone runs !setqueuechannel must change nothing.
    monkeypatch.setattr(
        items_bot, "_STATE", items_state.State(officer_channel_id=10)
    )
    ctx = FakeGuardCtx(999, "request")
    assert channel_guard.allows(999, items_bot.command_channels(ctx))


def test_every_registered_command_is_classified(monkeypatch):
    # Guards against a future command silently defaulting to unguarded.
    monkeypatch.setattr(items_bot, "_STATE", _configured_state())
    for command in items_bot.bot.commands:
        assert command.name in items_bot._CLASSIFIED_COMMANDS, (
            f"!{command.name} is not listed in items_bot's channel map"
        )
```

Add `import channel_guard` to the imports at the top of `tests/test_items_bot.py`.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -q -k "queue_channel or raffle_commands or usable_anywhere or unrestricted or classified"`
Expected: FAIL, `AttributeError: module 'items_bot' has no attribute 'command_channels'`

- [ ] **Step 3: Add the resolver and register the check**

Add `import channel_guard` alongside the other local imports at `items_bot.py:25`.

Insert after `_refuse_raffle` (i.e. before `@bot.command(name="setraffleroles")`, around `items_bot.py:447`):

```python
# Which channel each command belongs in. The !set*channel commands are
# exempt because they are how a channel is chosen; everything else is
# confined to the channel matching its audience -- members to the queue
# board, officers to the private officer channel, raffles to the raffle
# channel. Until the relevant channel is set the entry resolves to None
# and channel_guard leaves the command unrestricted.
_EXEMPT_COMMANDS = frozenset({
    "setofficerchannel", "setqueuechannel", "setrafflechannel",
})
_OFFICER_COMMANDS = frozenset({"distribute", "setraffleroles"})
_RAFFLE_COMMANDS = frozenset({"poll", "list", "winner"})
_QUEUE_COMMANDS = frozenset({"request", "cancelrequest", "myrequests", "itemhelp"})

_CLASSIFIED_COMMANDS = (
    _EXEMPT_COMMANDS | _OFFICER_COMMANDS | _RAFFLE_COMMANDS | _QUEUE_COMMANDS
)


def command_channels(ctx):
    """The channel ids ctx.command may run in, or EXEMPT."""
    name = ctx.command.name
    if name in _OFFICER_COMMANDS:
        return (_STATE.officer_channel_id,)
    if name in _RAFFLE_COMMANDS:
        return (_STATE.raffle_channel_id,)
    if name in _QUEUE_COMMANDS:
        return (_STATE.queue_channel_id,)
    # Exempt, and the deliberate default for anything unclassified: an
    # unlisted command keeps working everywhere rather than dying
    # silently. test_every_registered_command_is_classified is what stops
    # that default from quietly swallowing a new command.
    return channel_guard.EXEMPT


bot.add_check(channel_guard.make_check(command_channels))
```

- [ ] **Step 4: Swallow `WrongChannel` in the error handler**

In `on_command_error` (`items_bot.py:1261`), immediately after the `CommandNotFound` early return, add:

```python
    if isinstance(error, channel_guard.WrongChannel):
        # Silent on purpose: a reply would post into the very channel the
        # guard is keeping quiet, and would advertise that the command exists.
        return
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py tests/test_channel_guard.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Confine items commands to their own channels"
```

---

### Task 4: Timer bot — `tod_channel_id`, `!settodchannel`, and the guard

**Files:**
- Modify: `bot.py` (`load_local` `:113-121`, `data.setdefault` `:129-130`, `encode_state` `:160-168`, `decode_state` `:190-198`, new command after `:600`, resolver + check, new `on_command_error`)
- Test: `tests/test_bot.py` (new)

**Interfaces:**
- Consumes: `channel_guard.EXEMPT`, `make_check`, `WrongChannel`.
- Produces: `bot.command_channels(ctx)`, `data["tod_channel_id"]`.

`#fieldboss-tod-log` is a private officer-only channel where `!killed` is typed, and its ID has never been stored anywhere — without this setting the guard would break `!killed`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bot.py`:

```python
"""Timer bot: the TOD-log channel setting and the command guard.

Never call bot.save_local() or bot.persist() here -- data.json is a real
tracked file holding the live guild's channel ids.
"""

import channel_guard
import bot as timer_bot


class FakeGuardCtx:
    def __init__(self, channel_id, command_name):
        self.channel = type("Channel", (), {"id": channel_id})()
        self.command = type(
            "Cmd", (), {"name": command_name, "qualified_name": command_name}
        )()


def _configured(monkeypatch, **overrides):
    data = {
        "channel_id": 100,
        "storage_channel_id": 300,
        "tod_channel_id": 200,
        "deaths": {},
        "notified": {},
        "spawned": {},
    }
    data.update(overrides)
    monkeypatch.setattr(timer_bot, "data", data)
    return data


def test_timer_commands_work_in_the_notify_and_tod_channels(monkeypatch):
    _configured(monkeypatch)
    for name in ("killed", "boss", "bosses", "timer"):
        allowed = timer_bot.command_channels(FakeGuardCtx(100, name))
        assert channel_guard.allows(100, allowed)
        assert channel_guard.allows(200, allowed)


def test_timer_commands_are_refused_in_the_storage_and_foreign_channels(monkeypatch):
    _configured(monkeypatch)
    allowed = timer_bot.command_channels(FakeGuardCtx(300, "killed"))
    assert not channel_guard.allows(300, allowed)
    assert not channel_guard.allows(999, allowed)


def test_setup_commands_stay_usable_anywhere(monkeypatch):
    _configured(monkeypatch)
    for name in (
        "setchannel", "setstoragechannel", "clearstoragechannel", "settodchannel"
    ):
        ctx = FakeGuardCtx(999, name)
        assert timer_bot.command_channels(ctx) is channel_guard.EXEMPT


def test_commands_are_unrestricted_before_a_tod_channel_is_set(monkeypatch):
    # channel_id alone is still a configured channel, so !killed binds to it.
    _configured(monkeypatch, tod_channel_id=None)
    allowed = timer_bot.command_channels(FakeGuardCtx(100, "killed"))
    assert channel_guard.allows(100, allowed)
    assert not channel_guard.allows(200, allowed)


def test_a_totally_unconfigured_timer_bot_allows_everything(monkeypatch):
    _configured(monkeypatch, channel_id=None, tod_channel_id=None)
    allowed = timer_bot.command_channels(FakeGuardCtx(999, "killed"))
    assert channel_guard.allows(999, allowed)


def test_tod_channel_survives_the_state_round_trip(monkeypatch):
    _configured(monkeypatch)
    decoded = timer_bot.decode_state(timer_bot.encode_state())
    assert decoded["tod_channel_id"] == 200
    assert decoded["channel_id"] == 100
    assert decoded["storage_channel_id"] == 300


def test_a_state_message_written_before_this_change_still_decodes():
    # Pinned messages already live in the guild without the new key; if this
    # regresses, every timer in the guild is lost on the next restart.
    legacy = (
        f"{timer_bot.STATE_MARKER} — bot storage, please don't delete this message.\n"
        '```json\n{"channel_id": 100, "storage_channel_id": null, '
        '"deaths": {}, "notified": {}, "spawned": {}}\n```'
    )
    decoded = timer_bot.decode_state(legacy)
    assert decoded is not None
    assert decoded["tod_channel_id"] is None
    assert decoded["channel_id"] == 100
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bot.py -q`
Expected: FAIL, `AttributeError: module 'bot' has no attribute 'command_channels'`

- [ ] **Step 3: Add the setting to state**

In `bot.py`, add `import channel_guard` next to the other imports (after `from dotenv import load_dotenv`, `bot.py:35`).

In `load_local`'s fallback dict (`bot.py:114-120`), add after `"storage_channel_id": None,`:

```python
        "tod_channel_id": None,
```

After `data.setdefault("storage_channel_id", None)` (`bot.py:130`), add:

```python
# Absent from data.json and from pinned state written before the channel
# guard existed; None keeps the guard inert until !settodchannel is run.
data.setdefault("tod_channel_id", None)
```

In `encode_state`'s payload (`bot.py:161-167`), add after the `storage_channel_id` line:

```python
        "tod_channel_id": data.get("tod_channel_id"),
```

In `decode_state`'s returned dict (`bot.py:191-198`), add after the `storage_channel_id` line:

```python
            # Absent in messages written before the channel guard existed.
            "tod_channel_id": payload.get("tod_channel_id"),
```

- [ ] **Step 4: Add `!settodchannel`**

Insert after `clearstoragechannel` (`bot.py:600`, before `@bot.command(name="killed")`):

```python
@bot.command(name="settodchannel")
@commands.has_permissions(administrator=True)
async def settodchannel(ctx: commands.Context):
    """Also take commands in this channel: !settodchannel"""
    data["tod_channel_id"] = ctx.channel.id
    await persist()
    await ctx.send(
        embed=make_embed(
            "✅ TOD Log Channel Set",
            f"I'll accept commands in {ctx.channel.mention} as well as the "
            "notification channel, and ignore them everywhere else.",
            footer="Run this in a different channel to move it.",
        )
    )
```

Note the administrator gate: the other setup commands here are ungated, but this one is new (so gating it changes no existing behavior) and it is exempt from the guard, which makes it the one command that must not be member-runnable.

- [ ] **Step 5: Add the resolver, the check, and an error handler**

Insert immediately before `@bot.command(name="setchannel")` (`bot.py:549`):

```python
# The setup commands must work anywhere -- they are how a channel gets
# chosen. Everything else is confined to the notification channel and the
# TOD log. The storage channel is deliberately excluded: it exists only to
# hold the pinned state message.
_EXEMPT_COMMANDS = frozenset({
    "setchannel", "setstoragechannel", "clearstoragechannel", "settodchannel",
})


def command_channels(ctx):
    """The channel ids ctx.command may run in, or EXEMPT."""
    if ctx.command.name in _EXEMPT_COMMANDS:
        return channel_guard.EXEMPT
    return (data.get("channel_id"), data.get("tod_channel_id"))


bot.add_check(channel_guard.make_check(command_channels))


@bot.event
async def on_command_error(ctx, error):
    """Keep the log readable; this bot has no user-facing error replies.

    CommandNotFound is swallowed because all three bots share the "!"
    prefix, so most commands reaching this one belong to another bot --
    it already fills the logs with tracebacks today. WrongChannel is
    swallowed silently on purpose: replying would post into the very
    channel the guard is keeping quiet.
    """
    if isinstance(error, (commands.CommandNotFound, channel_guard.WrongChannel)):
        return
    print(f"Command {ctx.command} failed: {error!r}", file=sys.stderr, flush=True)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_bot.py tests/test_channel_guard.py -q`
Expected: all pass.

- [ ] **Step 7: Verify data.json was not rewritten**

Run: `git diff --stat data.json`
Expected: no output. If `data.json` changed, a test called `save_local`/`persist` — revert it with `git checkout data.json` and fix the test.

- [ ] **Step 8: Commit**

```bash
git add bot.py tests/test_bot.py
git commit -m "Confine timer commands to the notify and TOD-log channels"
```

---

### Task 5: Attendance bot — `attendance_channel_id`, the guard, and the `!setweek` gate

**Files:**
- Modify: `attendance_bot.py` (imports; config helpers near `:474`; `on_ready` `:877`; `on_command_error` `:882`; `!setweek` `:1323`; new command after `:1355`)
- Test: `tests/test_attendance_bot.py`

**Interfaces:**
- Consumes: `channel_guard.EXEMPT`, `make_check`, `WrongChannel`; `attendance_sheet.read_config`, `write_config`.
- Produces: `attendance_bot.command_channels(ctx)`, `attendance_bot.ATTENDANCE_CHANNEL_KEY`, `attendance_bot._ATTENDANCE_CHANNEL_ID`, `attendance_bot._load_attendance_channel()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_attendance_bot.py`:

```python
import channel_guard


class FakeGuardCtx:
    def __init__(self, channel_id, command_name):
        self.channel = type("Channel", (), {"id": channel_id})()
        self.command = type(
            "Cmd", (), {"name": command_name, "qualified_name": command_name}
        )()


def test_attendance_commands_are_confined_once_a_channel_is_set(monkeypatch):
    monkeypatch.setattr(attendance_bot, "_ATTENDANCE_CHANNEL_ID", 55)
    for name in (
        "attendance", "undoattendance", "setweek", "setofficerrole", "attendancehelp"
    ):
        allowed = attendance_bot.command_channels(FakeGuardCtx(55, name))
        assert channel_guard.allows(55, allowed)
        assert not channel_guard.allows(999, allowed)


def test_setattendancechannel_stays_usable_anywhere(monkeypatch):
    monkeypatch.setattr(attendance_bot, "_ATTENDANCE_CHANNEL_ID", 55)
    ctx = FakeGuardCtx(999, "setattendancechannel")
    assert attendance_bot.command_channels(ctx) is channel_guard.EXEMPT


def test_attendance_is_unrestricted_until_a_channel_is_set(monkeypatch):
    # Until !setattendancechannel is run, this bot behaves exactly as before.
    monkeypatch.setattr(attendance_bot, "_ATTENDANCE_CHANNEL_ID", None)
    allowed = attendance_bot.command_channels(FakeGuardCtx(999, "attendance"))
    assert channel_guard.allows(999, allowed)


def test_the_channel_is_read_from_the_config_tab(monkeypatch):
    monkeypatch.setattr(
        attendance_bot,
        "read_config",
        lambda _s: {attendance_bot.ATTENDANCE_CHANNEL_KEY: " 77 "},
    )
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: object())
    assert attendance_bot._load_attendance_channel() == 77


def test_a_missing_or_junk_config_value_leaves_the_guard_inert(monkeypatch):
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: object())
    monkeypatch.setattr(attendance_bot, "read_config", lambda _s: {})
    assert attendance_bot._load_attendance_channel() is None
    monkeypatch.setattr(
        attendance_bot,
        "read_config",
        lambda _s: {attendance_bot.ATTENDANCE_CHANNEL_KEY: "not-a-number"},
    )
    assert attendance_bot._load_attendance_channel() is None


def test_setweek_requires_administrator():
    checks = attendance_bot.set_week_cmd.checks
    assert checks, "!setweek must carry a permissions check"


def test_wrong_channel_is_swallowed_but_other_check_failures_still_reply():
    # The regression most likely to be missed: on_command_error replies to
    # CheckFailure, and WrongChannel is one.
    import asyncio
    from discord.ext import commands as dpy_commands

    # FakeCtx(author, attachments=()) records (args, kwargs, msg) tuples.
    ctx = FakeCtx(SimpleNamespace(id=1, roles=[], display_name="Keith"))
    ctx.command = None
    asyncio.run(
        attendance_bot.on_command_error(ctx, channel_guard.WrongChannel("nope"))
    )
    assert ctx.sent == []

    asyncio.run(
        attendance_bot.on_command_error(ctx, dpy_commands.CheckFailure("nope"))
    )
    assert ctx.sent, "a plain CheckFailure must still be reported"
```

`FakeCtx` is defined at `tests/test_attendance_bot.py:906`; it requires an
`author` and appends `(args, kwargs, msg)` tuples to `.sent`. `SimpleNamespace`
is already imported in that module.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_attendance_bot.py -q -k "confined or usable_anywhere or unrestricted or config_tab or inert or administrator or swallowed"`
Expected: FAIL, `AttributeError: module 'attendance_bot' has no attribute 'command_channels'`

- [ ] **Step 3: Add the config key, cache, and loader**

Add `import channel_guard` to `attendance_bot.py`'s imports. Confirm `read_config` and `write_config` are already imported from `attendance_sheet`; if not, add them to that import list.

Insert after `_set_officer_roles` (`attendance_bot.py:715`):

```python
ATTENDANCE_CHANNEL_KEY = "attendance_channel_id"

# Cached rather than read per command: the guard runs on every command, and
# !attendancehelp does no sheet I/O today -- reading the Config tab each time
# would spend Sheets quota on every keystroke. Refreshed on startup and
# whenever !setattendancechannel runs, so editing the cell by hand needs a
# restart. None means unconfigured, which leaves the guard inert.
_ATTENDANCE_CHANNEL_ID: int | None = None


def _load_attendance_channel() -> int | None:
    """The configured channel id, or None if unset or unreadable."""
    raw = read_config(_spreadsheet()).get(ATTENDANCE_CHANNEL_KEY, "").strip()
    return int(raw) if raw.isdigit() else None


def _set_attendance_channel(channel_id: int) -> None:
    """Record the channel this bot answers in. Run only via _locked."""
    write_config(_spreadsheet(), ATTENDANCE_CHANNEL_KEY, str(channel_id))


_EXEMPT_COMMANDS = frozenset({"setattendancechannel"})


def command_channels(ctx):
    """The channel ids ctx.command may run in, or EXEMPT."""
    if ctx.command.name in _EXEMPT_COMMANDS:
        return channel_guard.EXEMPT
    return (_ATTENDANCE_CHANNEL_ID,)


bot.add_check(channel_guard.make_check(command_channels))
```

- [ ] **Step 4: Load the cache on startup**

Replace `on_ready` (`attendance_bot.py:877-879`) with:

```python
async def on_ready():
    global _ATTENDANCE_CHANNEL_ID
    try:
        _ATTENDANCE_CHANNEL_ID = await asyncio.to_thread(_load_attendance_channel)
    except Exception as exc:
        # Leave the cache as-is (None on a cold start) so the guard stays
        # inert. A bot that answers everywhere beats one that answers nowhere.
        print(f"Could not read the attendance channel: {exc!r}", flush=True)
    print(f"Attendance bot logged in as {bot.user} (ID: {bot.user.id}).", flush=True)
```

- [ ] **Step 5: Swallow `WrongChannel`**

In `on_command_error` (`attendance_bot.py:882`), immediately after the `CommandNotFound` early return and **before** the `print(...)` to stderr, add:

```python
    if isinstance(error, channel_guard.WrongChannel):
        # Silent, and not even logged: with three bots on one prefix this
        # would otherwise be the noisiest line in the log.
        return
```

- [ ] **Step 6: Add `!setattendancechannel` and gate `!setweek`**

Add the administrator gate to `!setweek` by inserting one decorator line between `@bot.command(name="setweek")` and `async def set_week_cmd` (`attendance_bot.py:1323-1324`):

```python
@commands.has_permissions(administrator=True)
```

The existing `_require_officer` call inside stays; both must now pass.

Insert after `set_officer_role_cmd` ends (before `@bot.command(name="attendancehelp")`, `attendance_bot.py:1399`):

```python
@bot.command(name="setattendancechannel")
@commands.has_permissions(administrator=True)
async def set_attendance_channel_cmd(ctx: commands.Context):
    """Only answer attendance commands here: !setattendancechannel"""
    global _ATTENDANCE_CHANNEL_ID
    try:
        await _locked(_set_attendance_channel, ctx.channel.id)
    except Exception as exc:
        await _reject(ctx, "❌ Couldn't Save That", error_text(exc))
        return

    _ATTENDANCE_CHANNEL_ID = ctx.channel.id
    await ctx.send(
        embed=make_embed(
            "✅ Attendance Channel Set",
            f"I'll only answer attendance commands in {ctx.channel.mention} "
            "and ignore them everywhere else.",
            footer="Run this in a different channel to move it.",
        )
    )
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_attendance_bot.py tests/test_channel_guard.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add attendance_bot.py tests/test_attendance_bot.py
git commit -m "Confine attendance commands to one channel; gate !setweek"
```

---

### Task 6: Full verification and documentation

**Files:**
- Modify: `README.md` or `docs/item-bot-setup.md` (setup commands section)

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/python -m pytest -q 2>&1 | tail -5`
Expected: **0 failed**. Compare the passed count against the Task 2 figure plus the tests added in Tasks 3–5.

- [ ] **Step 2: Confirm no live state file was touched**

Run: `git status --short`
Expected: no `data.json` entry.

- [ ] **Step 3: Confirm every bot still imports**

Run: `.venv/bin/python -c "import bot, attendance_bot, items_bot; print('all three import')"`
Expected: `all three import`

- [ ] **Step 4: Document the two setup commands**

Add to the setup docs a short section: after deploying, run `!settodchannel` in `#fieldboss-tod-log` and `!setattendancechannel` in the attendance channel. Note that until they are run, both bots accept commands everywhere exactly as before, and that `items_bot` needs no new setup.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Document the channel-guard setup commands"
```

---

## Post-Implementation Notes

Not done here, deliberately, and worth a follow-up: `!setchannel`, `!setstoragechannel`, and `!clearstoragechannel` in `bot.py` carry no permission check, so any member can repoint the timer bot's notification and storage channels. That predates this work; gating them is a behavior change that was explicitly out of scope.
