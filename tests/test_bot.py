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
