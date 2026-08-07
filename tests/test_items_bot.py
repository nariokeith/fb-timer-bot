"""Tests for the item bot's Discord wiring.

No Discord client is started and nothing touches the network; the fakes
below stand in for the handful of discord.py objects the handlers use,
following the local-fakes style of test_attendance_bot.py.
"""

import asyncio

import pytest

import items_bot
import items_state


class FakeMessage:
    def __init__(self, content="", author_is_bot=True, message_id=1):
        self.content = content
        self.id = message_id
        self.pinned = False
        self.edits: list[str] = []

        class _Author:
            bot = author_is_bot

        self.author = _Author()

    async def edit(self, content=None, **kwargs):
        if content is not None:
            self.content = content
            self.edits.append(content)

    async def pin(self):
        self.pinned = True


class FakeChannel:
    def __init__(self, channel_id=99, pins=None):
        self.id = channel_id
        self._pins = list(pins or [])
        self.sent: list[str] = []

    def pins(self, limit=50):
        """An async iterator, matching discord.py 2.7's real signature.

        Modelling this as a coroutine returning a list would let
        `await channel.pins()` pass in tests and blow up in production
        with TypeError, taking restart recovery with it.
        """

        async def _iterator():
            for message in list(self._pins)[:limit]:
                yield message

        return _iterator()

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        message = FakeMessage(content=content or "", message_id=len(self.sent))
        self._pins.append(message)
        return message


@pytest.fixture(autouse=True)
def reset_module_state():
    """items_bot keeps _STATE and _STATE_MESSAGE at module level.

    Without this, a test that saves state leaves _STATE_MESSAGE pointing
    at a previous test's fake message, and the next save_state edits
    that instead of posting to its own channel -- so tests pass or fail
    depending on the order they run in.
    """
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGE = None
    yield
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGE = None


def test_missing_credentials_lists_every_absent_name():
    missing = items_bot.missing_credentials({})
    assert "ITEMS_DISCORD_TOKEN" in missing
    assert "ITEMS_SHEET_ID" in missing
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in missing


def test_missing_credentials_is_empty_when_all_present():
    assert items_bot.missing_credentials(
        {
            "ITEMS_DISCORD_TOKEN": "t",
            "ITEMS_SHEET_ID": "s",
            "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
        }
    ) == []


def test_exit_code_matches_the_supervisors_leave_it_stopped_code():
    assert items_bot.EXIT_NOT_CONFIGURED == 78


def test_gear_cap_defaults_to_three(monkeypatch):
    monkeypatch.delenv("ITEMS_GEAR_DAILY_CAP", raising=False)
    assert items_bot.gear_cap() == 3


def test_gear_cap_is_overridable(monkeypatch):
    monkeypatch.setenv("ITEMS_GEAR_DAILY_CAP", "5")
    assert items_bot.gear_cap() == 5


def test_a_nonsense_gear_cap_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ITEMS_GEAR_DAILY_CAP", "banana")
    assert items_bot.gear_cap() == 3


def test_a_module_level_lock_exists():
    assert isinstance(items_bot._SHEET_LOCK, asyncio.Lock)


def test_save_then_load_restores_the_queue():
    channel = FakeChannel()
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = [
        items_state.PendingRequest(
            id="aaa", user_id=1, ign="Kobe", item="Asta's Heart",
            type="Special", requested_at="2026-08-07 09:00:00",
        )
    ]

    asyncio.run(items_bot.save_state(channel))
    items_bot._STATE.queue = []
    asyncio.run(items_bot.load_state(channel))

    assert [r.id for r in items_bot._STATE.queue] == ["aaa"]


def test_dropped_requests_are_removed_from_memory_too():
    """Storage and memory must not disagree about what is queued."""
    channel = FakeChannel()
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = [
        items_state.PendingRequest(
            id=f"id{n:03d}", user_id=1, ign="Kobe",
            item="A Very Long Item Name Indeed",
            type="Gear", requested_at="2026-08-07 09:00:00",
        )
        for n in range(300)
    ]

    dropped = asyncio.run(items_bot.save_state(channel))

    assert dropped
    surviving = {r.id for r in items_bot._STATE.queue}
    assert not any(d.id in surviving for d in dropped)


def test_saving_twice_edits_the_same_message_rather_than_posting_again():
    channel = FakeChannel()
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = []

    asyncio.run(items_bot.save_state(channel))
    asyncio.run(items_bot.save_state(channel))

    assert len(channel.sent) == 1


def test_is_officer_channel_only_matches_the_recorded_channel():
    items_bot._STATE.officer_channel_id = 99
    assert items_bot.is_officer_channel(99)
    assert not items_bot.is_officer_channel(100)


def test_is_officer_channel_is_false_before_setup():
    items_bot._STATE.officer_channel_id = None
    assert not items_bot.is_officer_channel(99)
