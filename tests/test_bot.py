"""Timer bot: the TOD-log channel setting and the command guard.

Never call bot.save_local() or bot.persist() here -- data.json is a real
tracked file holding the live guild's channel ids.
"""

import os

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
        "setchannel",
        "setstoragechannel",
        "clearstoragechannel",
        "settodchannel",
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


def test_settodchannel_requires_administrator():
    assert timer_bot.settodchannel.checks, (
        "!settodchannel is exempt from the guard, so it must not be member-runnable"
    )


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


def test_the_timer_reports_a_missing_token_with_the_not_configured_code():
    """sys.exit(str) exits 1, and the timer is set to ALWAYS restart.

    bot.py's ChildSpec carries no_restart_codes=frozenset() so that an
    ordinary bot.run() return is relaunched. That makes exit 1 a permanent
    crash-loop: without a token the timer can never start, so the
    supervisor would respawn it every few seconds forever, backing off to
    the 300s cap and burying the log. 78 is the code the supervisor reads
    as "stopped on purpose, leave it alone" -- what the other two bots
    already use for the same situation.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["DISCORD_TOKEN"] = ""
    result = subprocess.run(
        [sys.executable, "bot.py"],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 78, (
        f"exited {result.returncode}; stderr: {result.stderr[-300:]}"
    )
    assert "DISCORD_TOKEN" in (result.stderr + result.stdout)


# ---------------------------------------------------------------------------
# !timer — the countdown must not edit its message once per second.
#
# The old loop issued one PATCH per second for up to 3600 seconds, which
# made it by far the highest-volume path in the codebase and put it hard
# against the per-channel edit bucket items_bot.py:193 measures at roughly
# five per five seconds. It also logged nothing on success, so an hour of
# that traffic was invisible in Render's logs.
# ---------------------------------------------------------------------------


class FakeTimerMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


class FakeTimerCtx:
    def __init__(self):
        self.sent = []
        self.author = type("Author", (), {"mention": "@keith"})()
        self.message = FakeTimerMessage()

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return self.message


def _run_timer(monkeypatch, seconds):
    """Run !timer with sleeping stubbed out, returning (ctx, sleep durations)."""
    import asyncio

    slept = []

    async def fake_sleep(duration):
        slept.append(duration)

    monkeypatch.setattr(timer_bot.asyncio, "sleep", fake_sleep)
    ctx = FakeTimerCtx()
    asyncio.run(timer_bot.timer.callback(ctx, str(seconds)))
    return ctx, slept


def test_the_countdown_edits_its_message_once_not_once_per_second(monkeypatch):
    ctx, _ = _run_timer(monkeypatch, 300)

    assert len(ctx.message.edits) == 1, (
        f"{len(ctx.message.edits)} edits for a 300s timer; the countdown "
        "should be rendered by Discord, not by repeated PATCHes"
    )


def test_the_countdown_is_a_discord_timestamp_so_it_ticks_without_api_calls(monkeypatch):
    ctx, _ = _run_timer(monkeypatch, 300)

    description = ctx.sent[0]["embed"].description
    assert ":R>" in description, (
        f"countdown body is {description!r}; it should use <t:...:R> markup, "
        "which every Discord client updates on its own"
    )


def test_the_countdown_sleeps_once_for_the_whole_duration(monkeypatch):
    _, slept = _run_timer(monkeypatch, 300)

    assert slept == [300]


def test_the_finished_timer_still_pings_the_author(monkeypatch):
    """Regression guard: the rewrite must not drop the notification."""
    ctx, _ = _run_timer(monkeypatch, 300)

    assert "@keith" in ctx.message.edits[-1]["embed"].description
