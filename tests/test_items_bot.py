"""Tests for the item bot's Discord wiring.

No Discord client is started and nothing touches the network; the fakes
below stand in for the handful of discord.py objects the handlers use,
following the local-fakes style of test_attendance_bot.py.
"""

import asyncio
import dataclasses
import datetime

import discord
import pytest
from discord.ext import commands

import channel_guard
import items_bot
import items_state


class FakeMessage:
    def __init__(
        self,
        content="",
        author_is_bot=True,
        message_id=1,
        *,
        raise_on_edit=False,
        raise_on_delete=False,
        raise_on_end_poll=False,
        raise_on_pin=False,
    ):
        self.content = content
        self.id = message_id
        self.pinned = False
        self.deleted = False
        self.edits: list[str] = []
        self.edit_calls = 0
        self.end_poll_calls = 0
        self.pin_calls = 0
        self.raise_on_edit = raise_on_edit
        self.raise_on_delete = raise_on_delete
        self.raise_on_end_poll = raise_on_end_poll
        self.raise_on_pin = raise_on_pin
        self.poll_ended = False
        self.embed = None
        self.view = None
        self.poll = None

        class _Author:
            bot = author_is_bot

        self.author = _Author()

    async def edit(self, content=None, **kwargs):
        self.edit_calls += 1
        if self.raise_on_edit:
            raise _http_exception()
        if content is not None:
            self.content = content
            self.edits.append(content)
        if "embed" in kwargs:
            self.embed = kwargs["embed"]
        if "view" in kwargs:
            self.view = kwargs["view"]

    async def pin(self):
        self.pin_calls += 1
        if self.raise_on_pin:
            raise _http_exception()
        self.pinned = True

    async def delete(self):
        if self.raise_on_delete:
            raise _http_exception()
        self.deleted = True

    async def end_poll(self):
        self.end_poll_calls += 1
        if self.raise_on_end_poll:
            raise _http_exception()
        self.poll_ended = True


def _http_exception():
    response = type("Response", (), {"status": 500, "reason": "Server Error"})()
    return discord.HTTPException(response, "Discord failed")


def _not_found():
    response = type("Response", (), {"status": 404, "reason": "Not Found"})()
    return discord.NotFound(response, "Discord message not found")


class FakeChannel:
    def __init__(self, channel_id=99, pins=None, history=None, *, raise_on_send=False):
        self.id = channel_id
        self.mention = f"#channel-{channel_id}"
        self._pins = list(pins or [])
        self._history = list(history or [])
        self.sent: list[FakeMessage] = []
        self.raise_on_send = raise_on_send

    def pins(self, limit=50):
        """An async iterator, matching discord.py 2.7's real signature.

        Modelling this as a coroutine returning a list would let
        `await channel.pins()` pass in tests and blow up in production
        with TypeError, taking restart recovery with it.
        """

        async def _iterator():
            for message in [m for m in self._pins if not m.deleted][:limit]:
                yield message

        return _iterator()

    def history(self, limit=100):
        async def _iterator():
            for message in [m for m in self._history if not m.deleted][:limit]:
                yield message

        return _iterator()

    async def fetch_message(self, message_id):
        for message in self.sent + self._pins + self._history:
            if message.id == message_id and not message.deleted:
                return message
        raise _not_found()

    async def send(self, content=None, **kwargs):
        if self.raise_on_send:
            raise _http_exception()
        message = FakeMessage(content=content or "", message_id=len(self.sent) + 1)
        message.embed = kwargs.get("embed")
        message.view = kwargs.get("view")
        message.poll = kwargs.get("poll")
        self.sent.append(message)
        self._pins.append(message)
        return message


class FakePollAnswer:
    def __init__(self, text, voters=()):
        self.text = text
        self._voters = list(voters)

    def voters(self, **kwargs):
        async def _iterator():
            for voter in self._voters:
                yield voter

        return _iterator()


class FakePoll:
    def __init__(self, question="Asta's Heart", answers=None, finalised=True):
        self.question = question
        self.answers = answers if answers is not None else [FakePollAnswer("Yes")]
        self._finalised = finalised

    def is_finalised(self):
        return self._finalised


@pytest.fixture(autouse=True)
def reset_module_state():
    """items_bot keeps _STATE and _STATE_MESSAGES at module level.

    Without this, a test that saves state leaves _STATE_MESSAGES pointing
    at previous tests' fake messages, and the next save_state edits
    that instead of posting to its own channel -- so tests pass or fail
    depending on the order they run in.
    """
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []
    items_bot._SHEET_LOCK = asyncio.Lock()
    if hasattr(items_bot, "_SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED"):
        items_bot._SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED = 0
    yield
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []
    items_bot._SHEET_LOCK = asyncio.Lock()
    if hasattr(items_bot, "_SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED"):
        items_bot._SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED = 0


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


def test_load_state_recovers_an_unpinned_state_message_from_history():
    state = items_state.State(officer_channel_id=99, queue=[
        items_state.PendingRequest("aaa", 1, "Kobe", "Asta's Heart", "Special", "2026-08-07 09:00:00")
    ])
    content = items_state.encode_state(state)[0]
    message = FakeMessage(content, message_id=3)
    channel = FakeChannel(history=[message])

    assert asyncio.run(items_bot.load_state(channel))
    assert [r.id for r in items_bot._STATE.queue] == ["aaa"]
    assert items_bot._STATE_MESSAGES == [message]


def test_saving_three_shards_then_one_deletes_the_surplus_messages():
    channel = FakeChannel()
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = [
        items_state.PendingRequest(
            id=f"id{n:03d}", user_id=n, ign=f"Player {n}",
            item="Asta's Heart", type="Special", requested_at="2026-08-07 09:00:00",
        )
        for n in range(30)
    ]

    asyncio.run(items_bot.save_state(channel))
    assert len(channel.sent) == 3
    surplus = list(items_bot._STATE_MESSAGES[1:])
    items_bot._STATE.queue = items_bot._STATE.queue[:1]
    asyncio.run(items_bot.save_state(channel))

    assert len(items_bot._STATE_MESSAGES) == 1
    assert all(message.deleted for message in surplus)


def test_saving_twice_edits_the_same_message_rather_than_posting_again():
    channel = FakeChannel()
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = []

    asyncio.run(items_bot.save_state(channel))
    asyncio.run(items_bot.save_state(channel))

    assert len(channel.sent) == 1


def test_appending_to_a_three_shard_queue_edits_only_the_last_shard():
    channel = FakeChannel()
    items_bot._STATE = items_state.State(
        officer_channel_id=channel.id,
        queue=[
            items_state.PendingRequest(
                id=f"id{n:03d}", user_id=n, ign=f"Player {n}",
                item="Asta's Heart", type="Special",
                requested_at="2026-08-07 09:00:00",
            )
            for n in range(30)
        ],
    )
    asyncio.run(items_bot.save_state(channel))
    messages = list(items_bot._STATE_MESSAGES)

    items_bot._STATE.queue.append(
        items_state.PendingRequest(
            id="appended", user_id=30, ign="Appended", item="Asta's Heart",
            type="Special", requested_at="2026-08-07 09:00:00",
        )
    )
    asyncio.run(items_bot.save_state(channel))

    assert len(messages) >= 3
    assert [message.edit_calls for message in messages] == [0, 0, 1]
    assert items_bot._STATE_MESSAGES == messages


def test_appending_that_creates_a_shard_rewrites_existing_shards_and_sends_one():
    channel = FakeChannel()
    items_bot._STATE = items_state.State(
        officer_channel_id=channel.id,
        queue=[
            items_state.PendingRequest(
                id=f"id{n:03d}", user_id=n, ign=f"Player {n}",
                item="Asta's Heart", type="Special",
                requested_at="2026-08-07 09:00:00",
            )
            for n in range(28)
        ],
    )
    asyncio.run(items_bot.save_state(channel))
    messages = list(items_bot._STATE_MESSAGES)

    items_bot._STATE.queue.append(
        items_state.PendingRequest(
            id="new-shard", user_id=28, ign="New Shard", item="Asta's Heart",
            type="Special", requested_at="2026-08-07 09:00:00",
        )
    )
    asyncio.run(items_bot.save_state(channel))

    assert [message.edit_calls for message in messages] == [1, 1]
    assert len(channel.sent) == 3
    assert len(items_bot._STATE_MESSAGES) == 3


def test_removing_a_middle_request_rewrites_its_shard_and_every_shard_after_it():
    channel = FakeChannel()
    items_bot._STATE = items_state.State(
        officer_channel_id=channel.id,
        queue=[
            items_state.PendingRequest(
                id=f"id{n:03d}", user_id=n, ign=f"Player {n}",
                item="Asta's Heart", type="Special",
                requested_at="2026-08-07 09:00:00",
            )
            for n in range(35)
        ],
    )
    asyncio.run(items_bot.save_state(channel))
    messages = list(items_bot._STATE_MESSAGES)

    del items_bot._STATE.queue[15]
    asyncio.run(items_bot.save_state(channel))

    assert len(messages) == 3
    assert [message.edit_calls for message in messages] == [0, 1, 1]


def test_unchanged_multi_shard_state_issues_no_edits_or_sends():
    channel = FakeChannel()
    items_bot._STATE = items_state.State(
        officer_channel_id=channel.id,
        queue=[
            items_state.PendingRequest(
                id=f"id{n:03d}", user_id=n, ign=f"Player {n}",
                item="Asta's Heart", type="Special",
                requested_at="2026-08-07 09:00:00",
            )
            for n in range(30)
        ],
    )
    asyncio.run(items_bot.save_state(channel))
    messages = list(items_bot._STATE_MESSAGES)
    sent_before = len(channel.sent)

    asyncio.run(items_bot.save_state(channel))

    assert [message.edit_calls for message in messages] == [0, 0, 0]
    assert len(channel.sent) == sent_before


def test_save_state_edits_an_empty_cached_message_even_when_the_state_is_unchanged():
    channel = FakeChannel()
    items_bot._STATE = items_state.State(
        officer_channel_id=channel.id,
        queue=[
            items_state.PendingRequest(
                id=f"id{n:03d}", user_id=n, ign=f"Player {n}",
                item="Asta's Heart", type="Special",
                requested_at="2026-08-07 09:00:00",
            )
            for n in range(30)
        ],
    )
    asyncio.run(items_bot.save_state(channel))
    messages = list(items_bot._STATE_MESSAGES)
    messages[1].content = ""

    asyncio.run(items_bot.save_state(channel))

    assert [message.edit_calls for message in messages] == [0, 1, 0]
    assert len(channel.sent) == 3


def test_save_load_round_trip_keeps_shards_in_part_order_for_the_next_save():
    channel = FakeChannel()
    items_bot._STATE = items_state.State(
        officer_channel_id=channel.id,
        queue=[
            _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(50)
        ],
    )

    asyncio.run(items_bot.save_state(channel))
    messages = list(reversed(items_bot._STATE_MESSAGES))
    channel = FakeChannel(channel.id, pins=messages)
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []

    assert asyncio.run(items_bot.load_state(channel))
    assert [items_state.decode_state(message.content).part for message in items_bot._STATE_MESSAGES] == list(
        range(len(messages))
    )
    asyncio.run(items_bot.save_state(channel))

    assert channel.sent == []
    assert all(message.edit_calls == 0 for message in items_bot._STATE_MESSAGES)


def test_load_state_with_a_missing_shard_restores_the_rest_and_warns():
    state = items_state.State(
        officer_channel_id=99,
        queue=[
            _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(30)
        ],
    )
    contents = items_state.encode_state(state)
    channel = FakeChannel(99, pins=[
        FakeMessage(content, message_id=part)
        for part, content in enumerate(contents)
        if part != 1
    ])

    assert asyncio.run(items_bot.load_state(channel))
    assert items_bot._STATE.missing_parts == (1,)
    assert len(items_bot._STATE.queue) < len(state.queue)
    assert "1 state shard" in channel.sent[-1].embed.description.lower()


def test_load_state_keeps_the_newest_duplicate_part_and_deletes_the_stale_one():
    fresh = items_state.State(
        officer_channel_id=99,
        queue=[
            _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(30)
        ],
    )
    fresh_contents = items_state.encode_state(fresh)
    stale = FakeMessage(
        items_state.encode_state(
            items_state.State(
                officer_channel_id=99,
                queue=[_queued("resolved", "Resolved", "Asta's Heart", items_rules.SPECIAL)],
            )
        )[0],
        message_id=100,
    )
    winners = [
        FakeMessage(content, message_id=200 + part)
        for part, content in enumerate(fresh_contents)
    ]
    channel = FakeChannel(99, pins=[stale, *reversed(winners)])

    assert asyncio.run(items_bot.load_state(channel))

    assert [request.id for request in items_bot._STATE.queue] == [
        request.id for request in fresh.queue
    ]
    assert stale.deleted
    assert items_bot._STATE_MESSAGES == winners


def test_load_state_discards_obsolete_surplus_shards_after_a_shrink():
    channel = FakeChannel()
    items_bot._STATE = items_state.State(
        officer_channel_id=channel.id,
        queue=[
            _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(30)
        ],
    )
    asyncio.run(items_bot.save_state(channel))
    assert len(channel.sent) == 3

    first = items_bot._STATE_MESSAGES[0]
    obsolete = items_bot._STATE_MESSAGES[1:]
    for message in obsolete:
        message.raise_on_delete = True
    items_bot._STATE.queue = items_bot._STATE.queue[:1]
    asyncio.run(items_bot.save_state(channel))
    assert all(not message.deleted for message in obsolete)

    for message in obsolete:
        message.raise_on_delete = False
    restored_channel = FakeChannel(channel.id, pins=[first, *obsolete])
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []

    assert asyncio.run(items_bot.load_state(restored_channel))
    assert [request.id for request in items_bot._STATE.queue] == ["id000"]


def test_load_state_deletes_obsolete_surplus_shards():
    current = items_state.State(
        officer_channel_id=99,
        queue=[_queued("current", "Current", "Asta's Heart", items_rules.SPECIAL)],
    )
    obsolete_state = items_state.State(
        officer_channel_id=99,
        queue=[
            _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(30)
        ],
    )
    current_message = FakeMessage(items_state.encode_state(current)[0], message_id=10)
    obsolete = [
        FakeMessage(content, message_id=20 + part)
        for part, content in enumerate(items_state.encode_state(obsolete_state)[1:], start=1)
    ]

    assert asyncio.run(items_bot.load_state(FakeChannel(99, pins=[current_message, *obsolete])))

    assert all(message.deleted for message in obsolete)


def test_load_state_keeps_every_part_of_a_current_multi_shard_state():
    state = items_state.State(
        officer_channel_id=99,
        queue=[
            _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(30)
        ],
    )
    messages = [
        FakeMessage(content, message_id=part)
        for part, content in enumerate(items_state.encode_state(state))
    ]

    assert asyncio.run(items_bot.load_state(FakeChannel(99, pins=messages)))

    assert [request.id for request in items_bot._STATE.queue] == [
        request.id for request in state.queue
    ]
    assert not any(message.deleted for message in messages)


def test_load_state_without_part_zero_uses_the_largest_total_and_warns():
    two_part_state = items_state.State(
        officer_channel_id=99,
        queue=[
            _queued(f"two-{n:03d}", f"Two {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(20)
        ],
    )
    three_part_state = items_state.State(
        officer_channel_id=99,
        queue=[
            _queued(f"three-{n:03d}", f"Three {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(30)
        ],
    )
    part_one = FakeMessage(items_state.encode_state(two_part_state)[1], message_id=1)
    part_two = FakeMessage(items_state.encode_state(three_part_state)[2], message_id=2)
    channel = FakeChannel(99, pins=[part_one, part_two])

    assert asyncio.run(items_bot.load_state(channel))

    assert items_bot._STATE.missing_parts == (0,)
    assert "1 state shard" in channel.sent[-1].embed.description.lower()
    assert not part_one.deleted
    assert not part_two.deleted


def test_save_state_replaces_and_deletes_a_message_whose_edit_failed():
    channel = FakeChannel()
    failed_message = FakeMessage("old", raise_on_edit=True)
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE_MESSAGES = [failed_message]

    asyncio.run(items_bot.save_state(channel))

    assert len(channel.sent) == 1
    assert failed_message.deleted
    assert items_bot._STATE_MESSAGES == channel.sent


def test_save_state_ignores_a_surplus_shard_delete_failure():
    channel = FakeChannel()
    items_bot._STATE = items_state.State(
        officer_channel_id=channel.id,
        queue=[
            _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(30)
        ],
    )
    asyncio.run(items_bot.save_state(channel))
    first = items_bot._STATE_MESSAGES[0]
    surplus = items_bot._STATE_MESSAGES[1:]
    surplus[0].raise_on_delete = True
    items_bot._STATE.queue = items_bot._STATE.queue[:1]

    asyncio.run(items_bot.save_state(channel))

    assert items_bot._STATE_MESSAGES == [first]
    assert not surplus[0].deleted


def test_save_state_clears_recovery_missing_parts_after_a_complete_rewrite():
    state = items_state.State(
        officer_channel_id=99,
        queue=[
            _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
            for n in range(30)
        ],
    )
    channel = FakeChannel(99, pins=[
        FakeMessage(content, message_id=part)
        for part, content in enumerate(items_state.encode_state(state))
        if part != 1
    ])

    assert asyncio.run(items_bot.load_state(channel))
    assert items_bot._STATE.missing_parts == (1,)
    asyncio.run(items_bot.save_state(channel))

    assert items_bot._STATE.missing_parts == ()


def test_is_officer_channel_only_matches_the_recorded_channel():
    items_bot._STATE.officer_channel_id = 99
    assert items_bot.is_officer_channel(99)
    assert not items_bot.is_officer_channel(100)


def test_is_officer_channel_is_false_before_setup():
    items_bot._STATE.officer_channel_id = None
    assert not items_bot.is_officer_channel(99)


def test_refresh_board_is_a_noop_without_a_configured_queue_channel(monkeypatch):
    channel = FakeChannel()
    items_bot._STATE.queue_channel_id = None
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: channel)

    asyncio.run(items_bot.refresh_board())

    assert channel.sent == []


def test_refresh_board_edits_the_existing_message_with_queue_order(monkeypatch):
    board = FakeMessage("old board", message_id=7)
    channel = FakeChannel(99, pins=[board])
    items_bot._STATE.queue_channel_id = channel.id
    items_bot._STATE.board_message_id = board.id
    items_bot._STATE.queue = [
        _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
        _queued("b", "Kobe", "Asta's Belt", items_rules.GEAR),
    ]
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: channel)

    asyncio.run(items_bot.refresh_board())

    assert board.edit_calls == 1
    assert board.embed.title == "📦 Queue Board"
    assert "1   Dajz" in board.embed.description
    assert "2   Kobe" in board.embed.description


def test_refresh_board_pins_an_existing_unpinned_message_after_updating_it(monkeypatch):
    board = FakeMessage("old board", message_id=7)
    channel = FakeChannel(99, pins=[board])
    items_bot._STATE.queue_channel_id = channel.id
    items_bot._STATE.board_message_id = board.id
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: channel)

    asyncio.run(items_bot.refresh_board())

    assert board.embed.title == "📦 Queue Board"
    assert "1   Dajz" in board.embed.description
    assert board.pinned
    assert board.pin_calls == 1


def test_refresh_board_does_not_repin_an_existing_pinned_message(monkeypatch):
    board = FakeMessage("old board", message_id=7)
    board.pinned = True
    channel = FakeChannel(99, pins=[board])
    items_bot._STATE.queue_channel_id = channel.id
    items_bot._STATE.board_message_id = board.id
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: channel)

    asyncio.run(items_bot.refresh_board())

    assert board.embed.title == "📦 Queue Board"
    assert board.pin_calls == 0


def test_refresh_board_updates_an_existing_message_when_pin_retry_fails(monkeypatch):
    board = FakeMessage("old board", message_id=7, raise_on_pin=True)
    channel = FakeChannel(99, pins=[board])
    items_bot._STATE.queue_channel_id = channel.id
    items_bot._STATE.board_message_id = board.id
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: channel)

    asyncio.run(items_bot.refresh_board())

    assert board.embed.title == "📦 Queue Board"
    assert "1   Dajz" in board.embed.description
    assert board.pin_calls == 1
    assert not board.pinned


def test_refresh_board_reposts_and_pins_a_deleted_message_and_saves_its_id(monkeypatch):
    state_channel = FakeChannel(77)
    board_channel = FakeChannel(88)
    items_bot._STATE.officer_channel_id = state_channel.id
    items_bot._STATE.queue_channel_id = board_channel.id
    items_bot._STATE.board_message_id = 7
    monkeypatch.setattr(
        items_bot.bot,
        "get_channel",
        lambda channel_id: {state_channel.id: state_channel, board_channel.id: board_channel}.get(channel_id),
    )

    asyncio.run(items_bot.refresh_board())

    board = board_channel.sent[0]
    saved = items_state.decode_state(state_channel.sent[0].content).state
    assert board.pinned
    assert items_bot._STATE.board_message_id == board.id
    assert saved.board_message_id == board.id


def _queue_board(monkeypatch, *, board=None, raise_on_send=False):
    state_channel = FakeChannel(77)
    board = board or FakeMessage("old board", message_id=7)
    board.pinned = True
    board_channel = FakeChannel(88, pins=[board], raise_on_send=raise_on_send)
    items_bot._STATE.officer_channel_id = state_channel.id
    items_bot._STATE.queue_channel_id = board_channel.id
    items_bot._STATE.board_message_id = board.id
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(
        items_bot.bot,
        "get_channel",
        lambda channel_id: {
            state_channel.id: state_channel,
            board_channel.id: board_channel,
        }.get(channel_id),
    )
    return state_channel, board_channel, board


def _queue_successes(monkeypatch, count):
    requests = []

    def accepted(_argument, user_id, _snapshot, _state, *, cap, today):
        number = len(requests) + 1
        request = items_state.PendingRequest(
            id=f"queued-{number}",
            user_id=user_id,
            ign=f"Player {number}",
            item="Asta's Belt",
            type=items_rules.GEAR,
            requested_at="2026-08-07 09:00:00",
        )
        requests.append(request)
        return items_bot.RequestOutcome(True, "Queued", request)

    monkeypatch.setattr(items_bot, "evaluate_request", accepted)
    contexts = []
    for number in range(count):
        ctx = FakeCtx(FakeChannel(number + 1), user_id=number + 1)
        asyncio.run(items_bot.request_cmd.callback(ctx, argument="anything"))
        contexts.append(ctx)
    return contexts


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


def test_a_refused_request_does_not_advance_the_board_repost_cadence(monkeypatch):
    _, board_channel, board = _queue_board(monkeypatch)
    items_bot._SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED = items_bot.BOARD_REPOST_EVERY - 2
    ctx = FakeCtx(FakeChannel(1))

    asyncio.run(items_bot.request_cmd.callback(ctx, argument="Asta's Heart Kobe"))

    _queue_successes(monkeypatch, 1)

    assert ctx.sent[-1]["embed"].title == "❌ Request refused"
    assert not board.deleted
    assert board.edit_calls == 1
    assert board_channel.sent == []


def test_approvals_denials_and_cancellations_do_not_repost_the_board(monkeypatch):
    _, board_channel, board = _queue_board(monkeypatch)
    items_bot._SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED = items_bot.BOARD_REPOST_EVERY - 1
    items_bot._STATE.queue = [_queued("deny", "Dajz", "Asta's Heart", items_rules.SPECIAL)]

    asyncio.run(items_bot.deny("deny"))

    items_bot._STATE.queue = [_queued("approve", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    monkeypatch.setattr(items_sheet, "commit_approval", lambda spreadsheet, **kwargs: "B3")
    asyncio.run(items_bot.approve("approve", "Keith"))

    items_bot._STATE.queue = [_queued("cancel", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    asyncio.run(items_bot.cancelrequest_cmd.callback(FakeCtx(FakeChannel(1))))

    assert board.edit_calls == 3
    assert not board.deleted
    assert board_channel.sent == []
    assert items_bot._SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED == items_bot.BOARD_REPOST_EVERY - 1


def test_a_repost_delete_failure_still_posts_a_new_board_and_keeps_the_request(monkeypatch):
    board = FakeMessage("old board", message_id=7, raise_on_delete=True)
    _, board_channel, old_board = _queue_board(monkeypatch, board=board)
    items_bot._SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED = items_bot.BOARD_REPOST_EVERY - 1

    ctx = _queue_successes(monkeypatch, 1)[0]

    assert not old_board.deleted
    assert len(board_channel.sent) == 1
    assert board_channel.sent[0].pinned
    assert items_bot._STATE.board_message_id == board_channel.sent[0].id
    assert ctx.sent[-1]["embed"].title == "✅ Request queued"


def test_a_failed_repost_send_leaves_the_board_ready_for_the_next_refresh(monkeypatch):
    _, board_channel, old_board = _queue_board(monkeypatch, raise_on_send=True)
    items_bot._SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED = items_bot.BOARD_REPOST_EVERY - 1

    ctx = _queue_successes(monkeypatch, 1)[0]

    assert old_board.deleted
    assert items_bot._STATE.board_message_id is None
    assert ctx.sent[-1]["embed"].title == "✅ Request queued"

    board_channel.raise_on_send = False
    asyncio.run(items_bot.refresh_board())

    replacement = board_channel.sent[0]
    assert replacement.pinned
    assert items_bot._STATE.board_message_id == replacement.id
    assert old_board.edit_calls == 0


def test_setqueuechannel_deletes_the_previous_board_message(monkeypatch):
    old_board = FakeMessage("old board", message_id=7)
    old_channel = FakeChannel(88, pins=[old_board])
    new_channel = FakeChannel(99)
    state_channel = FakeChannel(77)
    items_bot._STATE.officer_channel_id = state_channel.id
    items_bot._STATE.queue_channel_id = old_channel.id
    items_bot._STATE.board_message_id = old_board.id
    monkeypatch.setattr(
        items_bot.bot,
        "get_channel",
        lambda channel_id: {
            old_channel.id: old_channel,
            new_channel.id: new_channel,
            state_channel.id: state_channel,
        }.get(channel_id),
    )
    ctx = FakeCtx(new_channel)

    asyncio.run(items_bot.setqueuechannel_cmd.callback(ctx))

    assert old_board.deleted
    assert items_bot._STATE.queue_channel_id == new_channel.id
    assert new_channel.sent[0].pinned
    assert ctx.sent[-1]["embed"].title == "✅ Queue channel set"


def test_setqueuechannel_requires_an_officer_channel_before_posting_a_board(monkeypatch):
    queue_channel = FakeChannel(99)
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: queue_channel)
    ctx = FakeCtx(queue_channel)

    asyncio.run(items_bot.setqueuechannel_cmd.callback(ctx))

    assert items_bot._STATE.queue_channel_id is None
    assert items_bot._STATE.board_message_id is None
    assert len(queue_channel.sent) == 1
    assert queue_channel.sent[0].embed.title == "❌ Not set up yet"
    assert "!setofficerchannel" in queue_channel.sent[0].embed.description


def test_setqueuechannel_does_not_delete_the_previous_board_without_an_officer_channel(monkeypatch):
    previous_board = FakeMessage("old board", message_id=7)
    previous_channel = FakeChannel(88, pins=[previous_board])
    queue_channel = FakeChannel(99)
    items_bot._STATE.queue_channel_id = previous_channel.id
    items_bot._STATE.board_message_id = previous_board.id
    monkeypatch.setattr(
        items_bot.bot,
        "get_channel",
        lambda channel_id: {
            previous_channel.id: previous_channel,
            queue_channel.id: queue_channel,
        }.get(channel_id),
    )
    ctx = FakeCtx(queue_channel)

    asyncio.run(items_bot.setqueuechannel_cmd.callback(ctx))

    assert not previous_board.deleted
    assert items_bot._STATE.queue_channel_id == previous_channel.id
    assert items_bot._STATE.board_message_id == previous_board.id
    assert len(queue_channel.sent) == 1
    assert queue_channel.sent[0].embed.title == "❌ Not set up yet"


def test_setqueuechannel_posts_pins_and_saves_the_new_board_with_an_officer_channel(monkeypatch):
    state_channel = FakeChannel(77)
    queue_channel = FakeChannel(99)
    items_bot._STATE.officer_channel_id = state_channel.id
    monkeypatch.setattr(
        items_bot.bot,
        "get_channel",
        lambda channel_id: {
            state_channel.id: state_channel,
            queue_channel.id: queue_channel,
        }.get(channel_id),
    )
    ctx = FakeCtx(queue_channel)

    asyncio.run(items_bot.setqueuechannel_cmd.callback(ctx))

    board = queue_channel.sent[0]
    saved = items_state.decode_state(state_channel.sent[0].content).state
    assert board.pinned
    assert items_bot._STATE.queue_channel_id == queue_channel.id
    assert items_bot._STATE.board_message_id == board.id
    assert saved.queue_channel_id == queue_channel.id
    assert saved.board_message_id == board.id
    assert ctx.sent[-1]["embed"].title == "✅ Queue channel set"


class FakeCtx:
    def __init__(self, channel, user_id=1):
        self.channel = channel
        self.author = type("Author", (), {"id": user_id})()
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return await self.channel.send(**kwargs)


class FakeInteraction:
    def __init__(self, message, user_id=1):
        self.message = message
        self.channel_id = 99
        self.user = type("User", (), {"id": user_id, "display_name": "Keith"})()
        self.followups = []
        self.response = self.Response(self)
        self.followup = self.Followup(self)

    class Response:
        def __init__(self, interaction):
            self.interaction = interaction

        async def defer(self):
            pass

        async def send_message(self, *args, **kwargs):
            pass

        async def edit_message(self, **kwargs):
            await self.interaction.message.edit(**kwargs)

    class Followup:
        def __init__(self, interaction):
            self.interaction = interaction

        async def send(self, message):
            self.interaction.followups.append(message)


def test_moving_officer_channel_discards_the_old_state_messages(monkeypatch):
    old_messages = [FakeMessage("old"), FakeMessage("old")]
    items_bot._STATE.officer_channel_id = 10
    items_bot._STATE_MESSAGES = old_messages
    channel = FakeChannel(99)
    ctx = FakeCtx(channel)
    monkeypatch.setattr(items_bot, "save_state", _noop_save)

    asyncio.run(items_bot.setofficerchannel_cmd.callback(ctx))

    assert items_bot._STATE_MESSAGES == []
    assert items_bot._STATE.officer_channel_id == 99


def test_unreachable_officer_channel_does_not_keep_a_queued_request(monkeypatch):
    items_bot._STATE.officer_channel_id = 99
    items_bot._STATE.igns = {"1": "Kobe"}
    ctx = FakeCtx(FakeChannel(1))
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: None)

    asyncio.run(items_bot.request_cmd.callback(ctx, argument="Asta's Belt Dajz"))

    assert items_bot._STATE.queue == []
    assert items_bot._STATE.igns == {"1": "Kobe"}
    assert "unreachable" in ctx.sent[-1]["embed"].title.lower()


def test_request_that_would_exceed_state_capacity_is_refused_without_changes(monkeypatch):
    items_bot._STATE.officer_channel_id = 99
    # Sized off MAX_SHARDS rather than a literal, so raising the shard
    # ceiling cannot silently turn this into a test of the happy path.
    items_bot._STATE.queue = [
        _queued(f"id{n:03d}", f"Player {n}", "Asta's Belt", items_rules.GEAR)
        for n in range(items_state.MAX_SHARDS * 15)
    ]
    assert not items_state.fits(items_bot._STATE), "queue must start over the limit"
    items_bot._STATE.igns = {"1": "Kobe"}
    before_queue = list(items_bot._STATE.queue)
    before_igns = dict(items_bot._STATE.igns)
    ctx = FakeCtx(FakeChannel(1))
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)

    asyncio.run(items_bot.request_cmd.callback(ctx, argument="Asta's Belt Dajz"))

    assert items_bot._STATE.queue == before_queue
    assert items_bot._STATE.igns == before_igns
    assert "queue is full" in ctx.sent[-1]["embed"].title.lower()


def test_request_refreshes_the_queue_board(monkeypatch):
    officer_channel = FakeChannel(99)
    items_bot._STATE.officer_channel_id = officer_channel.id
    ctx = FakeCtx(FakeChannel(1))
    refreshes = []

    async def refresh():
        refreshes.append(True)

    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: officer_channel)
    monkeypatch.setattr(items_bot, "save_state", _noop_save)
    monkeypatch.setattr(items_bot, "refresh_board", refresh)

    asyncio.run(items_bot.request_cmd.callback(ctx, argument="Asta's Belt Dajz"))

    assert refreshes == [True]


def test_a_board_edit_failure_does_not_prevent_a_request_being_queued(monkeypatch):
    state_channel = FakeChannel(99)
    board = FakeMessage("old board", message_id=7, raise_on_edit=True)
    board_channel = FakeChannel(88, pins=[board])
    items_bot._STATE.officer_channel_id = state_channel.id
    items_bot._STATE.queue_channel_id = board_channel.id
    items_bot._STATE.board_message_id = board.id
    ctx = FakeCtx(FakeChannel(1))
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(
        items_bot.bot,
        "get_channel",
        lambda channel_id: {state_channel.id: state_channel, board_channel.id: board_channel}.get(channel_id),
    )

    asyncio.run(items_bot.request_cmd.callback(ctx, argument="Asta's Belt Dajz"))

    assert [request.ign for request in items_bot._STATE.queue] == ["Dajz"]
    assert ctx.sent[-1]["embed"].title == "✅ Request queued"


import items_rules
import items_sheet
import items_raffle

SPECIAL_HEADER_ROW = ["Player Name", "Asta's Heart", "Amentis' Foot"]

# Kobe already holds Asta's Heart; nobody else does.
SPECIAL_GRID_ROWS = [
    SPECIAL_HEADER_ROW,
    ["Kobe", "TRUE", "FALSE"],
    ["Dajz", "FALSE", "FALSE"],
    ["chinchong ni Mumu", "FALSE", "FALSE"],
]


def snapshot_with(ledger_rows=None, special_grid=None):
    return items_sheet.Snapshot(
        roster=["Kobe", "Dajz", "chinchong ni Mumu"],
        special_headers=SPECIAL_HEADER_ROW,
        gear_headers=["Player Name", "Asta's Belt", "Benji's Heart"],
        ledger_rows=ledger_rows
        if ledger_rows is not None
        else [
            ["2026-08-07 09:00:00", "Kobe", "Asta's Belt", "Gear", "O", "1", "aaa"],
            ["2026-08-07 10:00:00", "Kobe", "Benji's Heart", "Gear", "O", "1", "bbb"],
        ],
        special_grid=special_grid if special_grid is not None else SPECIAL_GRID_ROWS,
    )


SNAPSHOT = snapshot_with()


def _evaluate(argument, state=None, user_id=1, snapshot=None):
    return items_bot.evaluate_request(
        argument,
        user_id,
        snapshot if snapshot is not None else SNAPSHOT,
        state if state is not None else items_state.State(),
        cap=3,
        today="2026-08-07",
    )


def test_a_valid_gear_request_is_accepted():
    outcome = _evaluate("Asta's Belt Dajz")
    assert outcome.accepted
    assert outcome.request.item == "Asta's Belt"
    assert outcome.request.type == items_rules.GEAR


def test_a_multi_word_ign_is_accepted():
    outcome = _evaluate("Asta's Belt chinchong ni Mumu")
    assert outcome.accepted
    assert outcome.request.ign == "chinchong ni Mumu"


def test_a_special_log_is_refused_and_points_at_the_raffle():
    """Special logs are raffled now. !request must say where to go."""
    outcome = _evaluate("Asta's Heart Dajz")
    assert not outcome.accepted
    assert "raffled" in outcome.message.lower()


def test_a_gear_request_at_the_cap_is_refused_before_officers_see_it():
    ledger = SNAPSHOT.ledger_rows + [
        ["2026-08-07 11:00:00", "Kobe", "Asta's Belt", "Gear", "O", "1", "ccc"]
    ]
    outcome = _evaluate("Asta's Belt Kobe", snapshot=snapshot_with(ledger_rows=ledger))
    assert not outcome.accepted


def test_pending_gear_requests_count_toward_the_cap():
    state = items_state.State(
        queue=[
            items_state.PendingRequest(
                id="x", user_id=1, ign="Kobe", item="Asta's Belt",
                type=items_rules.GEAR, requested_at="2026-08-07 10:30:00",
            )
        ]
    )
    outcome = _evaluate("Benji's Heart Kobe", state=state)
    assert not outcome.accepted


def test_yesterdays_pending_gear_does_not_count_toward_todays_cap():
    state = items_state.State(queue=[
        items_state.PendingRequest(
            id="x", user_id=1, ign="Kobe", item="Asta's Belt",
            type=items_rules.GEAR, requested_at="2026-08-06 23:59:00",
        )
    ])
    outcome = _evaluate("Benji's Heart Kobe", state=state)
    assert outcome.accepted


def _queued(request_id, ign, item, type_):
    return items_state.PendingRequest(
        id=request_id, user_id=1, ign=ign, item=item, type=type_,
        requested_at="2026-08-07 09:00:00",
    )


def test_panel_lines_number_each_request_and_show_its_status():
    lines = items_bot.panel_lines(
        [
            _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
            _queued("b", "Kobe", "Asta's Belt", items_rules.GEAR),
        ],
        SNAPSHOT,
        cap=3,
        today="2026-08-07",
    )
    assert lines[0].startswith("**1.")
    assert "Dajz" in lines[0] and "Asta's Heart" in lines[0]
    assert "2/3" in lines[1], "a gear line shows how many the player used today"


def test_panel_lines_flag_a_player_at_the_cap():
    ledger = SNAPSHOT.ledger_rows + [
        ["2026-08-07 11:00:00", "Kobe", "Asta's Belt", "Gear", "O", "1", "ccc"]
    ]
    lines = items_bot.panel_lines(
        [_queued("b", "Kobe", "Asta's Belt", items_rules.GEAR)],
        snapshot_with(ledger_rows=ledger), cap=3, today="2026-08-07",
    )
    assert "⚠️" in lines[0]


def test_panel_lines_flag_a_special_the_player_already_holds():
    """Kobe already has Asta's Heart in SPECIAL_GRID_ROWS."""
    lines = items_bot.panel_lines(
        [_queued("a", "Kobe", "Asta's Heart", items_rules.SPECIAL)],
        SNAPSHOT, cap=3, today="2026-08-07",
    )
    assert "already has it" in lines[0]


def test_panel_lines_show_a_requests_note():
    request = items_state.PendingRequest(
        id="a", user_id=1, ign="Dajz", item="Asta's Heart",
        type=items_rules.SPECIAL, requested_at="2026-08-07 09:00:00",
        note="previously requested as Kobe",
    )
    lines = items_bot.panel_lines([request], SNAPSHOT, cap=3, today="2026-08-07")
    assert "previously requested as Kobe" in lines[0]


def test_panel_lines_number_from_the_page_start():
    lines = items_bot.panel_lines(
        [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)],
        SNAPSHOT, cap=3, today="2026-08-07", start=26,
    )
    assert lines[0].startswith("**26.")


def test_distribute_posts_one_message_for_a_sixty_request_queue(monkeypatch):
    items_bot._STATE.officer_channel_id = 99
    items_bot._STATE.queue = [
        _queued(f"id{n}", "Dajz", "Asta's Heart", items_rules.SPECIAL)
        for n in range(60)
    ]
    ctx = FakeCtx(FakeChannel(99))
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)

    asyncio.run(items_bot.distribute_cmd.callback(ctx))

    assert len(ctx.sent) == 1


def test_page_two_lists_requests_twenty_six_through_fifty():
    queue = [
        _queued(f"id{n}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
        for n in range(60)
    ]
    panel = items_bot.DistributePanel(queue, SNAPSHOT, cap=3, today="2026-08-07", page=1)

    assert [option.value for option in panel.picker.options] == [
        f"id{n}" for n in range(25, 50)
    ]
    embed = panel.build_embed()
    assert "**26. Player 25**" in embed.description
    assert "**50. Player 49**" in embed.description


def test_single_page_panel_has_no_page_buttons():
    panel = items_bot.DistributePanel(
        [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)],
        SNAPSHOT,
        cap=3,
        today="2026-08-07",
    )

    assert [child.label for child in panel.children if child.row == 2] == []


def test_a_panel_keeps_each_officers_selection_separate():
    panel = items_bot.DistributePanel(
        [
            _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
            _queued("b", "Kobe", "Asta's Belt", items_rules.GEAR),
        ],
        SNAPSHOT,
        cap=3,
        today="2026-08-07",
    )
    panel.selected[101] = "a"
    panel.selected[202] = "b"
    assert panel.selected[101] == "a"
    assert panel.selected[202] == "b"


def test_page_button_edits_the_existing_message_and_clears_selections():
    queue = [
        _queued(f"id{n}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
        for n in range(60)
    ]
    panel = items_bot.DistributePanel(queue, SNAPSHOT, cap=3, today="2026-08-07")
    channel = FakeChannel(99)
    message = asyncio.run(channel.send(embed=panel.build_embed(), view=panel))
    panel.message = message
    panel.selected[101] = "id0"
    interaction = FakeInteraction(message)
    page_two = next(child for child in panel.children if getattr(child, "label", None) == "2")

    asyncio.run(page_two.callback(interaction))

    assert len(channel.sent) == 1
    assert message.edit_calls == 1
    assert message.view.page == 1
    assert message.view.selected == {}


def test_resolving_the_only_request_on_the_last_page_returns_to_a_nonempty_page(monkeypatch):
    items_bot._STATE.officer_channel_id = 99
    items_bot._STATE.queue = [
        _queued(f"id{n}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
        for n in range(26)
    ]
    panel = items_bot.DistributePanel(
        list(items_bot._STATE.queue), SNAPSHOT, cap=3, today="2026-08-07", page=1
    )
    channel = FakeChannel(99)
    message = asyncio.run(channel.send(embed=panel.build_embed(), view=panel))
    panel.message = message
    panel.selected[1] = "id25"
    interaction = FakeInteraction(message)

    async def resolve(request_id, officer_name):
        items_state.remove_request(items_bot._STATE, request_id)
        return "Approved"

    monkeypatch.setattr(items_bot, "approve", resolve)
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    asyncio.run(panel.approve_button.callback(interaction))

    assert message.view.page == 0
    assert "**1. Player 0**" in message.embed.description


def test_an_empty_queue_says_so():
    embed = items_bot.build_panel_embed([], SNAPSHOT, cap=3, today="2026-08-07")
    assert "no pending" in embed.description.lower()


def test_deny_removes_the_request_and_writes_nothing():
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    message = asyncio.run(items_bot.deny("a"))
    assert items_bot._STATE.queue == []
    assert "Dajz" in message


def test_deny_refreshes_the_queue_board(monkeypatch):
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    refreshes = []

    async def refresh():
        refreshes.append(True)

    monkeypatch.setattr(items_bot, "refresh_board", refresh)

    asyncio.run(items_bot.deny("a"))

    assert refreshes == [True]


def test_denying_an_already_resolved_request_reports_it():
    items_bot._STATE.queue = []
    message = asyncio.run(items_bot.deny("gone"))
    assert "already" in message.lower()


def test_approving_an_already_resolved_request_writes_nothing(monkeypatch):
    items_bot._STATE.queue = []
    calls = []
    monkeypatch.setattr(
        items_sheet, "commit_approval", lambda *a, **k: calls.append(k)
    )
    message = asyncio.run(items_bot.approve("gone", "Keith"))
    assert calls == []
    assert "already" in message.lower()


def test_approve_removes_an_already_recorded_request_without_writing(monkeypatch):
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    recorded = snapshot_with(ledger_rows=SNAPSHOT.ledger_rows + [
        ["2026-08-07 11:00:00", "Dajz", "Asta's Heart", "Special", "O", "1", "a"]
    ])
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: recorded)
    monkeypatch.setattr(items_bot, "save_state", _noop_save)
    calls = []
    refreshes = []

    async def refresh():
        refreshes.append(True)

    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(items_bot, "refresh_board", refresh)

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert calls == []
    assert items_bot._STATE.queue == []
    assert refreshes == [True]
    assert "already recorded" in message.lower()


def test_approve_commits_and_removes_the_request(monkeypatch):
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    calls = []
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(
        items_sheet, "commit_approval",
        lambda spreadsheet, **kwargs: calls.append(kwargs) or "B3",
    )
    monkeypatch.setattr(items_bot, "save_state", _noop_save)

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert len(calls) == 1
    assert calls[0]["ign"] == "Dajz"
    assert calls[0]["officer"] == "Keith"
    assert items_bot._STATE.queue == []
    assert "Dajz" in message


def test_approve_refreshes_the_queue_board(monkeypatch):
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    refreshes = []

    async def refresh():
        refreshes.append(True)

    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(items_sheet, "commit_approval", lambda spreadsheet, **kwargs: "B3")
    monkeypatch.setattr(items_bot, "save_state", _noop_save)
    monkeypatch.setattr(items_bot, "refresh_board", refresh)

    asyncio.run(items_bot.approve("a", "Keith"))

    assert refreshes == [True]


def test_board_edit_failure_does_not_prevent_an_approval_completing(monkeypatch):
    state_channel = FakeChannel(99)
    board = FakeMessage("old board", message_id=7, raise_on_edit=True)
    board_channel = FakeChannel(88, pins=[board])
    items_bot._STATE.officer_channel_id = state_channel.id
    items_bot._STATE.queue_channel_id = board_channel.id
    items_bot._STATE.board_message_id = board.id
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    commits = []
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(
        items_sheet,
        "commit_approval",
        lambda spreadsheet, **kwargs: commits.append(kwargs) or "B3",
    )
    monkeypatch.setattr(
        items_bot.bot,
        "get_channel",
        lambda channel_id: {state_channel.id: state_channel, board_channel.id: board_channel}.get(channel_id),
    )

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert len(commits) == 1
    assert items_bot._STATE.queue == []
    assert message == "Approved **Asta's Heart** for **Dajz**."


def test_board_content_updates_after_an_approval(monkeypatch):
    state_channel = FakeChannel(99)
    board = FakeMessage("old board", message_id=7)
    board_channel = FakeChannel(88, pins=[board])
    items_bot._STATE.officer_channel_id = state_channel.id
    items_bot._STATE.queue_channel_id = board_channel.id
    items_bot._STATE.board_message_id = board.id
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(items_sheet, "commit_approval", lambda spreadsheet, **kwargs: "B3")
    monkeypatch.setattr(
        items_bot.bot,
        "get_channel",
        lambda channel_id: {state_channel.id: state_channel, board_channel.id: board_channel}.get(channel_id),
    )

    asyncio.run(items_bot.approve("a", "Keith"))

    assert "Nothing pending" in board.embed.description
    assert "Dajz" not in board.embed.description


def test_approve_rechecks_the_cap_and_refuses_a_stale_request(monkeypatch):
    """The second cap check is the point of this test.

    The request passed the check when it was queued. By the time an
    officer clicks, the player has been given their third gear log by
    hand, so the approval must be refused even though the request is
    still sitting in the queue.
    """
    items_bot._STATE.queue = [_queued("a", "Kobe", "Asta's Belt", items_rules.GEAR)]
    full_ledger = SNAPSHOT.ledger_rows + [
        ["2026-08-07 11:00:00", "Kobe", "Benji's Heart", "Gear", "O", "1", "ccc"]
    ]
    monkeypatch.setattr(
        items_sheet, "read_snapshot",
        lambda spreadsheet: snapshot_with(ledger_rows=full_ledger),
    )
    calls = []
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(items_bot, "save_state", _noop_save)
    # approve() reads the real clock, but the ledger rows above are dated.
    # Without pinning "today" the cap resets and this test passes vacuously
    # from 2026-08-08 onward.
    monkeypatch.setattr(items_bot, "today_pht", lambda: "2026-08-07")

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert calls == [], "no write when the cap is already reached"
    assert items_bot._STATE.queue, "a refused approval leaves the request queued"
    assert "limit" in message.lower()


def test_a_failed_sheet_write_leaves_the_request_queued(monkeypatch):
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)

    def boom(*args, **kwargs):
        raise RuntimeError("sheets is down")

    monkeypatch.setattr(items_sheet, "commit_approval", boom)
    monkeypatch.setattr(items_bot, "save_state", _noop_save)

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert items_bot._STATE.queue, "nothing may be lost when the sheet fails"
    assert "sheets is down" in message


def test_a_partial_write_dequeues_and_hands_over_the_row(monkeypatch):
    """The cell is written; a retry would double-count.

    So the request must NOT stay queued, and the officers must be told
    exactly what to paste into the ledger.
    """
    items_bot._STATE.queue = [_queued("a", "Kobe", "Asta's Belt", items_rules.GEAR)]
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(items_bot, "save_state", _noop_save)

    row = ["2026-08-07 14:00:00", "Kobe", "Asta's Belt", "Gear", "Keith", "1", "a"]

    def partial(*args, **kwargs):
        raise items_sheet.LedgerWriteError("B2", row, RuntimeError("ledger is down"))

    monkeypatch.setattr(items_sheet, "commit_approval", partial)

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert items_bot._STATE.queue == [], "a written cell must not be re-approvable"
    assert "B2" in message
    assert "Asta's Belt" in message
    assert "do not approve this again" in message.lower()


async def _noop_save(channel=None):
    return []


def test_a_duplicate_pending_request_is_refused():
    state = items_state.State(
        queue=[
            items_state.PendingRequest(
                id="x", user_id=1, ign="Dajz", item="Asta's Belt",
                type=items_rules.GEAR, requested_at="2026-08-07 10:30:00",
            )
        ]
    )
    outcome = _evaluate("Asta's Belt Dajz", state=state)
    assert not outcome.accepted
    assert "pending" in outcome.message.lower()


def test_an_unparseable_request_is_refused_with_the_reason():
    outcome = _evaluate("Asta's Heart Nobody")
    assert not outcome.accepted
    assert "Nobody" in outcome.message


def test_an_ign_differing_from_last_time_is_noted_not_refused():
    """Requesting for an alt is legitimate; the officer judges it."""
    state = items_state.State(igns={"1": "Kobe"})
    outcome = _evaluate("Asta's Belt Dajz", state=state, user_id=1)
    assert outcome.accepted
    assert "Kobe" in outcome.request.note


def test_the_same_ign_as_last_time_carries_no_note():
    state = items_state.State(igns={"1": "Dajz"})
    outcome = _evaluate("Asta's Belt Dajz", state=state, user_id=1)
    assert outcome.accepted
    assert outcome.request.note == ""


def test_a_duplicate_is_refused_even_from_a_different_account():
    """Keyed on IGN, not on who asked."""
    state = items_state.State(
        queue=[
            items_state.PendingRequest(
                id="x", user_id=999, ign="Dajz", item="Asta's Belt",
                type=items_rules.GEAR, requested_at="2026-08-07 10:30:00",
            )
        ]
    )
    outcome = _evaluate("Asta's Heart Dajz", state=state, user_id=1)
    assert not outcome.accepted


def test_requests_for_user_returns_only_that_users_requests():
    items_bot._STATE.queue = [
        _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
        _queued("b", "Kobe", "Asta's Belt", items_rules.GEAR),
    ]
    items_bot._STATE.queue[1] = items_state.PendingRequest(
        id="b", user_id=2, ign="Kobe", item="Asta's Belt",
        type=items_rules.GEAR, requested_at="2026-08-07 09:00:00",
    )
    mine = items_bot.requests_for_user(items_bot._STATE, 1)
    assert [r.id for r in mine] == ["a"]


def test_cancellable_picks_the_only_pending_request():
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    found, error = items_bot.cancellable(items_bot._STATE, 1, "")
    assert found.id == "a"
    assert error is None


def test_cancellable_needs_a_name_when_several_are_pending():
    items_bot._STATE.queue = [
        _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
        _queued("b", "Dajz", "Amentis' Foot", items_rules.SPECIAL),
    ]
    found, error = items_bot.cancellable(items_bot._STATE, 1, "")
    assert found is None
    assert "Asta's Heart" in error


def test_cancellable_matches_by_item_name():
    items_bot._STATE.queue = [
        _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
        _queued("b", "Dajz", "Amentis' Foot", items_rules.SPECIAL),
    ]
    found, error = items_bot.cancellable(items_bot._STATE, 1, "amentis' foot")
    assert found.id == "b"


def test_cancellable_reports_when_nothing_is_pending():
    items_bot._STATE.queue = []
    found, error = items_bot.cancellable(items_bot._STATE, 1, "")
    assert found is None
    assert "no pending" in error.lower()


def test_cancelrequest_refreshes_the_queue_board(monkeypatch):
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    ctx = FakeCtx(FakeChannel(1))
    refreshes = []

    async def refresh():
        refreshes.append(True)

    monkeypatch.setattr(items_bot, "refresh_board", refresh)

    asyncio.run(items_bot.cancelrequest_cmd.callback(ctx))

    assert refreshes == [True]


def test_the_members_intent_is_enabled():
    """Poll voters resolve to Members only when this intent is on.

    Without it discord.py falls back to User objects, whose display_name
    is the global name -- not the 'BK | Jjew' server nickname the roster
    match depends on.
    """
    assert items_bot.intents.members is True


def test_dropping_special_requests_removes_only_the_specials():
    state = items_state.State(
        queue=[
            items_state.PendingRequest("a", 1, "Kobe", "Asta's Heart", "Special", "2026-08-09 09:00:00"),
            items_state.PendingRequest("b", 2, "Jjew", "Sacred Ring", "Gear", "2026-08-09 09:01:00"),
        ]
    )

    dropped = items_bot.drop_special_requests(state)

    assert [r.id for r in dropped] == ["a"]
    assert [r.id for r in state.queue] == ["b"]


def test_dropping_nothing_leaves_the_queue_alone():
    state = items_state.State(
        queue=[items_state.PendingRequest("b", 2, "Jjew", "Sacred Ring", "Gear", "2026-08-09 09:01:00")]
    )

    assert items_bot.drop_special_requests(state) == []
    assert [r.id for r in state.queue] == ["b"]


def test_announcing_dropped_specials_saves_and_names_every_member():
    channel = FakeChannel(99)
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = [
        items_state.PendingRequest("a", 7, "Kobe", "Asta's Heart", "Special", "2026-08-09 09:00:00"),
        items_state.PendingRequest("b", 8, "Jjew", "Asta's Belt", "Gear", "2026-08-09 09:01:00"),
    ]

    asyncio.run(items_bot.announce_dropped_specials(channel))

    notice = channel.sent[-1].embed
    assert "Asta's Heart" in notice.description
    assert "<@7>" in notice.description
    assert [r.id for r in items_bot._STATE.queue] == ["b"]
    saved = items_state.decode_shards([m.content for m in channel.sent if m.content])
    assert [r.id for r in saved.queue] == ["b"]


def test_announcing_nothing_posts_nothing():
    channel = FakeChannel(99)
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = [
        items_state.PendingRequest("b", 8, "Jjew", "Asta's Belt", "Gear", "2026-08-09 09:01:00")
    ]

    asyncio.run(items_bot.announce_dropped_specials(channel))

    assert channel.sent == []


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id
        self.mention = f"@role-{role_id}"


class FakeMember:
    def __init__(self, user_id=1, roles=(), display_name="BK | Jjew", administrator=False):
        self.id = user_id
        self.display_name = display_name
        self.roles = list(roles)
        self.guild_permissions = type(
            "Perms", (), {"administrator": administrator}
        )()


def _raffle_ctx(channel_id=42, roles=(10,), administrator=False):
    channel = _register_channel(FakeChannel(channel_id))
    ctx = FakeCtx(channel)
    ctx.author = FakeMember(roles=[FakeRole(r) for r in roles], administrator=administrator)
    return ctx, channel


def test_holding_any_configured_role_is_enough():
    author = FakeMember(roles=[FakeRole(99), FakeRole(11)])

    assert items_bot.has_raffle_role(author, [10, 11])


def test_holding_no_configured_role_is_refused():
    author = FakeMember(roles=[FakeRole(99)])

    assert not items_bot.has_raffle_role(author, [10, 11])


def test_setraffleroles_stores_every_role_once():
    items_bot._STATE.officer_channel_id = 1
    channel = FakeChannel(1)
    ctx = FakeCtx(channel)
    roles = (FakeRole(10), FakeRole(11), FakeRole(10))

    asyncio.run(items_bot.setraffleroles_cmd.callback(ctx, *roles))

    assert items_bot._STATE.raffle_role_ids == [10, 11]
    assert ctx.sent[-1]["embed"].title == "✅ Raffle roles set"


def test_setraffleroles_without_a_role_shows_usage():
    items_bot._STATE.officer_channel_id = 1
    ctx = FakeCtx(FakeChannel(1))

    asyncio.run(items_bot.setraffleroles_cmd.callback(ctx))

    assert items_bot._STATE.raffle_role_ids == []
    assert "!setraffleroles" in ctx.sent[-1]["embed"].description


def test_setrafflechannel_requires_an_officer_channel_first():
    ctx = FakeCtx(FakeChannel(42))

    asyncio.run(items_bot.setrafflechannel_cmd.callback(ctx))

    assert items_bot._STATE.raffle_channel_id is None
    assert "!setofficerchannel" in ctx.sent[-1]["embed"].description


def test_setrafflechannel_records_the_channel(monkeypatch):
    state_channel = FakeChannel(1)
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: state_channel)
    items_bot._STATE.officer_channel_id = 1
    ctx = FakeCtx(FakeChannel(42))

    asyncio.run(items_bot.setrafflechannel_cmd.callback(ctx))

    assert items_bot._STATE.raffle_channel_id == 42
    assert ctx.sent[-1]["embed"].title == "✅ Raffle channel set"


def test_an_unconfigured_raffle_channel_hints_only_to_admins():
    admin_ctx, _ = _raffle_ctx(administrator=True)
    member_ctx, _ = _raffle_ctx(administrator=False)

    assert "!setrafflechannel" in items_bot.raffle_access(admin_ctx)
    assert items_bot.raffle_access(member_ctx) is items_bot.IGNORE


def test_the_wrong_channel_is_silently_ignored():
    items_bot._STATE.raffle_channel_id = 42
    items_bot._STATE.raffle_role_ids = [10]
    ctx, _ = _raffle_ctx(channel_id=999)

    assert items_bot.raffle_access(ctx) is items_bot.IGNORE


def test_no_configured_roles_is_a_refusal_not_an_open_door():
    items_bot._STATE.raffle_channel_id = 42
    ctx, _ = _raffle_ctx()

    assert "!setraffleroles" in items_bot.raffle_access(ctx)


def test_a_member_without_a_raffle_role_is_refused():
    items_bot._STATE.raffle_channel_id = 42
    items_bot._STATE.raffle_role_ids = [10]
    ctx, _ = _raffle_ctx(roles=(99,))

    assert "role" in items_bot.raffle_access(ctx).casefold()


def test_a_role_holder_in_the_raffle_channel_is_permitted():
    items_bot._STATE.raffle_channel_id = 42
    items_bot._STATE.raffle_role_ids = [10]
    ctx, _ = _raffle_ctx()

    assert items_bot.raffle_access(ctx) is None


def _sheet(monkeypatch, special=("Player Name", "Asta's Heart"), gear=("Player Name", "Sacred Ring"), roster=("Jjew", "Kobe"), holds=()):
    snapshot = items_sheet.Snapshot(
        roster=list(roster),
        special_headers=list(special),
        gear_headers=list(gear),
        ledger_rows=[],
        special_grid=[],
    )
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: snapshot)
    monkeypatch.setattr(
        items_sheet, "holds_special",
        lambda snap, ign, item: ign in holds,
    )
    return snapshot


# Channels the fake bot can resolve by id, the way discord.py does.
# A get_channel that returns the same object for every id would hide
# whether the code looks a channel up correctly at all.
_CHANNELS: dict[int, FakeChannel] = {}


def _register_channel(channel):
    _CHANNELS[channel.id] = channel
    return channel


def _configured_raffle(monkeypatch, channel_id=42):
    state_channel = FakeChannel(1)
    _CHANNELS.clear()
    _register_channel(state_channel)
    items_bot._STATE.officer_channel_id = 1
    items_bot._STATE.raffle_channel_id = channel_id
    items_bot._STATE.raffle_role_ids = [10]
    monkeypatch.setattr(
        items_bot.bot, "get_channel", lambda cid: _CHANNELS.get(cid, state_channel)
    )
    return state_channel


def _posted_poll(channel):
    """The poll message, not the confirmation embed sent after it.

    poll_cmd sends two messages: the poll itself, then an ok_embed
    telling the officer when it closes. channel.sent[-1] is the latter.
    """
    polls = [message for message in channel.sent if message.poll is not None]
    assert len(polls) == 1, f"expected one poll message, got {len(polls)}"
    return polls[0]


def test_iam_binds_a_member_to_their_roster_row(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[], display_name="xXshadowXx")

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Kobe"))

    assert items_bot._STATE.bindings["7"] == "Kobe"
    assert ctx.sent[-1]["embed"].title.startswith("✅")


def test_iam_needs_no_raffle_role(monkeypatch):
    """Ordinary members must be able to identify themselves."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    assert items_bot._STATE.bindings["7"] == "Jjew"


def test_iam_is_silent_outside_the_raffle_channel(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(channel_id=999, roles=())

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    assert ctx.sent == []
    assert items_bot._STATE.bindings == {}


def test_iam_refuses_an_ign_another_account_already_claimed(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    items_bot._STATE.bindings["5"] = "Kobe"
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Kobe"))

    assert "already" in ctx.sent[-1]["embed"].description.casefold()
    assert items_bot._STATE.bindings == {"5": "Kobe"}


def test_iam_cannot_take_a_row_another_account_holds_under_an_alias(monkeypatch):
    """A roster rename leaves an alias spelling behind; it still resolves."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Kobe", "Jjew"))
    items_bot._STATE.bindings["5"] = "KobePH"
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Kobe"))

    assert items_bot._STATE.bindings == {"5": "KobePH"}
    assert "already" in ctx.sent[-1]["embed"].description.casefold()


def test_iam_lets_a_member_rebind_themselves(monkeypatch):
    """Changing main must not need an officer."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    items_bot._STATE.bindings["7"] = "Jjew"
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Kobe"))

    assert items_bot._STATE.bindings["7"] == "Kobe"


def test_iam_refuses_a_name_not_in_the_roster(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Nobody"))

    assert "No player named" in ctx.sent[-1]["embed"].description
    assert items_bot._STATE.bindings == {}


def test_iam_refuses_a_member_an_officer_marked_not_a_player(monkeypatch):
    """Letting them clear it themselves would make the mark meaningless."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    items_bot._STATE.not_players.append("7")
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    assert "officer" in ctx.sent[-1]["embed"].description.casefold()
    assert items_bot._STATE.bindings == {}


def test_iam_honours_a_not_a_player_mark_that_lands_while_it_waits(monkeypatch):
    """An officer can mark the caller while !iam is queued on the lock."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    async def scenario():
        await items_bot._SHEET_LOCK.acquire()
        task = asyncio.ensure_future(
            items_bot.iam_cmd.callback(ctx, argument="Jjew")
        )
        for _ in range(5):          # let !iam reach the lock and block there
            await asyncio.sleep(0)
        items_bot._STATE.not_players.append("7")   # what !notaplayer does
        items_bot._SHEET_LOCK.release()
        await task

    asyncio.run(scenario())

    assert items_bot._STATE.bindings == {}
    assert "officer" in ctx.sent[-1]["embed"].description.casefold()


def test_bind_overrides_another_accounts_claim(monkeypatch):
    """One IGN maps to at most one account."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    items_bot._STATE.bindings["5"] = "Kobe"
    ctx, _ = _raffle_ctx()

    asyncio.run(
        items_bot.bind_cmd.callback(ctx, FakeMember(user_id=7), argument="Kobe")
    )

    assert items_bot._STATE.bindings == {"7": "Kobe"}
    assert "5" in ctx.sent[-1]["embed"].description


def test_iam_is_announced_in_the_officer_channel(monkeypatch):
    """The one command a member can use to change state leaves a trail."""
    state_channel = _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    posted = " ".join(message.content for message in state_channel.sent)
    assert "<@7>" in posted and "Jjew" in posted


def test_a_failed_officer_notice_does_not_undo_the_binding(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])
    monkeypatch.setattr(
        items_bot,
        "_tell_officers",
        lambda text: (_ for _ in ()).throw(RuntimeError("channel gone")),
    )

    try:
        asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))
    except RuntimeError:
        pass

    assert items_bot._STATE.bindings["7"] == "Jjew"


def test_bind_displaces_a_claim_held_under_an_alias(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Kobe", "Jjew"))
    items_bot._STATE.bindings["5"] = "KobePH"
    ctx, _ = _raffle_ctx()

    asyncio.run(
        items_bot.bind_cmd.callback(ctx, FakeMember(user_id=7), argument="Kobe")
    )

    assert items_bot._STATE.bindings == {"7": "Kobe"}


def test_bind_clears_a_not_a_player_mark(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    items_bot._STATE.not_players.append("7")
    ctx, _ = _raffle_ctx()

    asyncio.run(
        items_bot.bind_cmd.callback(ctx, FakeMember(user_id=7), argument="Jjew")
    )

    assert items_bot._STATE.bindings["7"] == "Jjew"
    assert items_bot._STATE.not_players == []


def test_binding_the_unidentified_voter_retries_the_stuck_poll(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "xXshadowXx")]),
    )
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))
    assert ctx.sent[-1]["embed"].title == "❌ Pool not frozen"

    member = FakeMember(user_id=2)
    asyncio.run(items_bot.bind_cmd.callback(ctx, member, argument="Kobe"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is True, "the fix should have retried the freeze"
    assert "Asta's Heart" in ctx.sent[-1]["embed"].title


def test_a_binding_with_no_session_running_retries_nothing(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, _ = _raffle_ctx()
    member = FakeMember(user_id=2)

    asyncio.run(items_bot.bind_cmd.callback(ctx, member, argument="Kobe"))

    assert items_bot._STATE.raffle_session is None
    assert "🎲" not in str(ctx.sent[-1]["embed"].title)


def test_notaplayer_also_retries_the_stuck_poll(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "xXshadowXx")]),
    )
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    asyncio.run(items_bot.notaplayer_cmd.callback(ctx, FakeMember(user_id=2)))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is True


def test_bind_needs_a_raffle_role(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())

    asyncio.run(
        items_bot.bind_cmd.callback(ctx, FakeMember(user_id=7), argument="Jjew")
    )

    assert items_bot._STATE.bindings == {}


def test_notaplayer_marks_and_clears_any_binding(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, _ = _raffle_ctx()
    items_bot._STATE.bindings["7"] = "Jjew"

    asyncio.run(items_bot.notaplayer_cmd.callback(ctx, FakeMember(user_id=7)))

    assert items_bot._STATE.not_players == ["7"]
    assert items_bot._STATE.bindings == {}


def test_a_binding_that_will_not_fit_is_rolled_back(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    monkeypatch.setattr(items_state, "fits", lambda state: False)
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    assert items_bot._STATE.bindings == {}
    assert "full" in ctx.sent[-1]["embed"].description.casefold()


def test_poll_posts_a_poll_and_records_the_raffle(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    posted = _posted_poll(channel)
    assert posted.poll.question == "Asta's Heart"
    assert [a.text for a in posted.poll.answers] == ["Yes"]
    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.message_id == posted.id
    assert raffle.channel_id == channel.id
    assert raffle.listed is False


def test_poll_defaults_to_twenty_four_hours(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert _posted_poll(channel).poll.duration == datetime.timedelta(hours=24)


def test_poll_honours_the_hours_flag(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart --hours 48"))

    assert _posted_poll(channel).poll.duration == datetime.timedelta(hours=48)


def test_poll_refuses_a_gear_log(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Sacred Ring"))

    assert "!request" in ctx.sent[-1]["embed"].description
    assert items_bot._STATE.raffles == []


def test_poll_refuses_a_second_open_raffle_for_the_same_log(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))
    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert "already open" in ctx.sent[-1]["embed"].description
    assert len(items_bot._STATE.raffles) == 1


def test_poll_refuses_an_item_whose_draw_is_unfinished(monkeypatch):
    """A partly drawn raffle is unevictable and unsupersedable; a new poll
    would hide it behind the newer record forever."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe"),
        listed=True, winners=("Jjew",), drawn=False,
    )

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert "unfinished draw" in ctx.sent[-1]["embed"].description
    assert len(items_bot._STATE.raffles) == 1, "the unfinished raffle must survive"
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winners == ("Jjew",)


def _fill_every_raffle_slot(monkeypatch, ends="2099-01-01 00:00:00", already_drawn=()):
    logs = [f"Log {n}" for n in range(items_state.MAX_RAFFLES)]
    _sheet(monkeypatch, special=("Player Name", "Asta's Heart", *logs))
    items_bot._STATE.raffles = [
        items_state.Raffle(
            item=name, channel_id=42, message_id=n,
            created_at=f"2026-08-09 {n:02d}:00:00", ends_at=ends,
            winners=("Kobe",) if name in already_drawn else (),
            drawn=name in already_drawn,
        )
        for n, name in enumerate(logs)
    ]


def test_poll_refuses_when_every_slot_holds_a_live_raffle(monkeypatch):
    _configured_raffle(monkeypatch)
    _fill_every_raffle_slot(monkeypatch)
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert "waiting for a winner" in ctx.sent[-1]["embed"].description
    assert len(items_bot._STATE.raffles) == items_state.MAX_RAFFLES


def test_poll_refuses_rather_than_discard_an_ended_raffle_with_no_winner(monkeypatch):
    """The frozen pool is the only copy. Never drop it to make room."""
    _configured_raffle(monkeypatch)
    _fill_every_raffle_slot(monkeypatch, ends="2020-01-01 00:00:00")
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert "waiting for a winner" in ctx.sent[-1]["embed"].description
    assert len(items_bot._STATE.raffles) == items_state.MAX_RAFFLES
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is None


def test_poll_reuses_the_slot_of_the_oldest_drawn_raffle(monkeypatch):
    _configured_raffle(monkeypatch)
    _fill_every_raffle_slot(monkeypatch, already_drawn=("Log 0", "Log 3"))
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    items = [r.item for r in items_bot._STATE.raffles]
    assert "Log 0" not in items
    assert "Log 3" in items
    assert "Asta's Heart" in items
    assert len(items) == items_state.MAX_RAFFLES


def test_poll_outside_the_raffle_channel_says_nothing(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx(channel_id=999)

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert ctx.sent == []


class FakeVoter:
    def __init__(self, user_id, display_name):
        self.id = user_id
        self.display_name = display_name


def _open_raffle(channel, item="Asta's Heart", ends="2099-01-01 00:00:00", **kwargs):
    poll = kwargs.pop("poll", FakePoll(question=item))
    message = FakeMessage(message_id=555)
    message.poll = poll
    channel._pins.append(message)
    raffle = items_state.Raffle(
        item=item, channel_id=channel.id, message_id=message.id,
        created_at="2026-08-09 10:00:00", ends_at=ends, **kwargs,
    )
    items_bot._STATE.raffles.append(raffle)
    return raffle, message


def test_render_pool_shows_the_request_fallback_group():
    voter = items_raffle.Voter(user_id=3, display_name="xXshadowXx")
    split = items_raffle.VoterSplit(
        eligible=["Jjew", "Ryuu"], from_request=[(voter, "Ryuu")]
    )

    text = items_bot.render_pool("Asta's Heart", split)

    assert "last !request" in text
    assert "<@3>" in text
    assert "Ryuu" in text


def test_render_pool_names_two_accounts_on_one_player():
    voter = items_raffle.Voter(user_id=9, display_name="xXshadowXx")
    split = items_raffle.VoterSplit(eligible=["Jjew"], duplicates=[(voter, "Jjew")])

    text = items_bot.render_pool("Asta's Heart", split)

    assert "<@9>" in text
    assert "one player" in text


def _fake_poll_voters(pairs):
    """Stand in for poll_voters, which would otherwise hit Discord."""
    async def _voters(message):
        return [
            items_raffle.Voter(user_id=user_id, display_name=name)
            for user_id, name in pairs
        ]
    return _voters


def test_startraffle_refuses_when_no_poll_has_closed(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel)  # ends in 2099, still open

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert "no closed poll" in ctx.sent[-1]["embed"].description.lower()
    assert items_bot._STATE.raffle_session is None


def test_startraffle_walks_the_closed_polls_oldest_first(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, item="Log B", ends="2026-08-09 10:00:00",
        eligible=("Jjew",), listed=True,
    )
    items_bot._STATE.raffles[-1] = dataclasses.replace(
        items_bot._STATE.raffles[-1], created_at="2026-08-09 11:00:00"
    )
    _open_raffle(
        channel, item="Log A", ends="2026-08-09 10:00:00",
        eligible=("Jjew", "Kobe"), listed=True,
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    session = items_bot._STATE.raffle_session
    assert session.items == ("Log A", "Log B")
    assert session.position == 0
    description = ctx.sent[-1]["embed"].description
    assert "Jjew" in description and "Kobe" in description
    assert "🎲 Poll 1 of 2" in ctx.sent[-1]["embed"].title


def test_startraffle_freezes_a_pool_that_has_not_been_listed(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"), holds=("Kobe",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "Kobe")]),
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is True
    assert raffle.eligible == ("Jjew",)


def test_startraffle_holds_on_an_unidentified_voter(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "xXshadowXx")]),
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert ctx.sent[-1]["embed"].title == "❌ Pool not frozen"
    session = items_bot._STATE.raffle_session
    assert session is not None and session.position == 0
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is False


def test_startraffle_during_a_session_retries_the_current_poll(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert items_bot._STATE.raffle_session.items == ("Asta's Heart",)
    assert "Jjew" in ctx.sent[-1]["embed"].description


def test_a_session_with_one_poll_ends_after_it_is_drawn(monkeypatch):
    """Reaching the end posts the summary and clears the session."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=1,
        results=(("Asta's Heart", ("Kobe",)),),
    )

    asyncio.run(items_bot._post_current_poll(ctx))

    assert items_bot._STATE.raffle_session is None
    description = ctx.sent[-1]["embed"].description
    assert "Asta's Heart" in description and "Kobe" in description


def test_the_summary_marks_a_skipped_log(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=2,
        results=(("Log A", ("Kobe",)),), skipped=("Log B",),
    )

    asyncio.run(items_bot._post_current_poll(ctx))

    description = ctx.sent[-1]["embed"].description
    assert "Log B" in description and "skipped" in description.lower()


def test_render_pool_names_who_won_earlier_in_the_session():
    split = items_raffle.VoterSplit(eligible=["Jjew"])

    description = items_bot.render_pool(
        "Asta's Heart", split, won_this_session=["Kobe"]
    )

    assert "Kobe" in description
    assert "Won earlier this session" in description


def test_startraffle_replays_the_frozen_pool(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True
    )
    channel._pins[-1].poll = FakePoll(answers=[FakePollAnswer("Yes", [FakeVoter(9, "Kobe")])])

    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert "Jjew" in ctx.sent[-1]["embed"].description
    assert "Kobe" not in ctx.sent[-1]["embed"].description


def test_startraffle_refuses_when_the_poll_message_is_gone(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel, ends="2026-08-09 10:00:00")
    message.deleted = True

    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert "poll message" in ctx.sent[-1]["embed"].description


def test_startraffle_reuses_the_frozen_pool_after_poll_deletion(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True
    )
    message.deleted = True

    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert "Jjew" in ctx.sent[-1]["embed"].description


def test_cancelpoll_ends_and_deletes_an_open_poll(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel, poll=FakePoll(finalised=False))

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx, argument="Asta's Heart"))

    assert message.poll_ended
    assert message.deleted
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is None
    assert ctx.sent[-1]["embed"].title == "✅ Poll cancelled"


def test_cancelpoll_refuses_a_drawn_raffle_and_keeps_it(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, channel = _raffle_ctx()
    raffle, message = _open_raffle(channel, winners=("Jjew",), drawn=True)

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx, argument="Asta's Heart"))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is raffle
    assert not message.poll_ended
    assert not message.deleted
    assert "distribution history" in ctx.sent[-1]["embed"].description


def test_cancelpoll_refuses_a_closed_undrawn_raffle_and_keeps_it(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, channel = _raffle_ctx()
    raffle, message = _open_raffle(channel, ends="2026-08-09 10:00:00")

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx, argument="Asta's Heart"))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is raffle
    assert not message.poll_ended
    assert not message.deleted
    assert "!poll Asta's Heart" in ctx.sent[-1]["embed"].description


def test_cancelpoll_refuses_an_unknown_item_name(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel)

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx, argument="Benji's Heart"))

    assert "No tracked raffle" in ctx.sent[-1]["embed"].description
    assert "Asta's Heart" in ctx.sent[-1]["embed"].description


def test_cancelpoll_refuses_a_blank_argument(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx))

    assert "!cancelpoll <special log name>" in ctx.sent[-1]["embed"].description


def test_cancelpoll_drops_state_when_the_message_is_already_deleted(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel)
    message.deleted = True

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx, argument="Asta's Heart"))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is None
    assert "already gone" in ctx.sent[-1]["embed"].description
    assert ctx.sent[-1]["embed"].title == "⚠️ Poll cancelled, message missing"


def test_cancelpoll_drops_state_when_message_deletion_fails(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel, poll=FakePoll(finalised=False))
    message.raise_on_delete = True

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx, argument="Asta's Heart"))

    assert message.poll_ended
    assert not message.deleted
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is None
    assert "delete the poll message" in ctx.sent[-1]["embed"].description.casefold()
    assert ctx.sent[-1]["embed"].title == "⚠️ Poll cancelled, message left behind"


def test_cancelpoll_keeps_state_when_ending_the_poll_fails(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, channel = _raffle_ctx()
    raffle, message = _open_raffle(channel, poll=FakePoll(finalised=False))
    message.raise_on_end_poll = True

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx, argument="Asta's Heart"))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is raffle
    assert not message.deleted
    assert "Nothing was cancelled" in ctx.sent[-1]["embed"].description


def test_help_says_request_is_gear_only_and_lists_the_raffle_commands():
    ctx = FakeCtx(FakeChannel(1))

    asyncio.run(items_bot.itemhelp_cmd.callback(ctx))

    text = str(ctx.sent[-1]["embed"].to_dict())
    assert "gear" in text.casefold()
    for command in (
        "!poll", "!startraffle", "!won", "!skipraffle", "!cancelpoll",
        "!setraffleroles", "!setrafflechannel",
    ):
        assert command in text


def test_help_no_longer_offers_special_logs_through_request():
    """The old 'one per player, ever' line described a !request rule."""
    ctx = FakeCtx(FakeChannel(1))

    asyncio.run(items_bot.itemhelp_cmd.callback(ctx))

    text = str(ctx.sent[-1]["embed"].to_dict())
    assert "raffle" in text.casefold()
    assert "one per player, ever" not in text


def test_repolling_an_ended_undrawn_raffle_replaces_it(monkeypatch):
    """Otherwise the old one is unreachable AND unevictable: a leaked slot.

    find_raffle only ever returns the newest raffle for a name, so an
    older undrawn one can never be listed, drawn or evicted again -- it
    would occupy a slot until someone edited the pinned state by hand.
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))
    first = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    items_state.replace_raffle(items_bot._STATE, first, ends_at="2020-01-01 00:00:00")

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert len(items_bot._STATE.raffles) == 1
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").ends_at != "2020-01-01 00:00:00"
    assert "replaces" in ctx.sent[-1]["embed"].description.casefold()


def test_repolling_never_replaces_a_raffle_that_was_already_drawn(monkeypatch):
    """A drawn raffle is history worth keeping; a new poll sits beside it."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))
    first = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    items_state.replace_raffle(
        items_bot._STATE, first, ends_at="2020-01-01 00:00:00", winners=("Kobe",), drawn=True
    )

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert len(items_bot._STATE.raffles) == 2


class PollRejectingChannel(FakeChannel):
    """Rejects the poll but still delivers the error reply.

    A blanket raise_on_send would make the refusal embed fail too, which
    tells us nothing about whether the slot was consumed.
    """

    async def send(self, content=None, **kwargs):
        if kwargs.get("poll") is not None:
            raise _http_exception()
        return await super().send(content, **kwargs)


def test_a_failed_poll_post_does_not_consume_the_evicted_slot(monkeypatch):
    """Eviction must not be spent on a poll Discord never accepted."""
    _configured_raffle(monkeypatch)
    _fill_every_raffle_slot(monkeypatch, already_drawn=("Log 0",))
    before = [r.item for r in items_bot._STATE.raffles]
    channel = PollRejectingChannel(42)
    ctx = FakeCtx(channel)
    ctx.author = FakeMember(roles=[FakeRole(10)])

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert [r.item for r in items_bot._STATE.raffles] == before
    assert ctx.sent[-1]["embed"].title == "❌ Could not post the poll"


def test_startraffle_refuses_to_freeze_a_pool_it_could_never_save(monkeypatch):
    """An unsaveable freeze poisons every later save, not just this one.

    save_state gives up when encode_state raises, so an in-memory pool
    too big for a shard would stop the queue persisting at all -- the
    same guard !poll and !request already apply, applied here.
    """
    _configured_raffle(monkeypatch)
    roster = [f"AVeryLongPlayerName{n:03d}" for n in range(150)]
    _sheet(monkeypatch, roster=roster)
    ctx, channel = _raffle_ctx()
    raffle, message = _open_raffle(channel, ends="2026-08-09 10:00:00")
    message.poll = FakePoll(answers=[FakePollAnswer(
        "Yes", [FakeVoter(n, name) for n, name in enumerate(roster)]
    )])

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    unchanged = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert unchanged.listed is False
    assert unchanged.eligible == ()
    assert items_state.fits(items_bot._STATE)
    assert "too large" in ctx.sent[-1]["embed"].description.casefold()


def test_two_officers_starting_the_same_raffle_at_once(monkeypatch):
    """Concurrent session starts freeze one poll without replacing its state."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel, ends="2026-08-09 10:00:00")
    message.poll = FakePoll(answers=[FakePollAnswer("Yes", [FakeVoter(1, "BK | Jjew")])])

    original_fetch = channel.fetch_message

    async def slow_fetch(message_id):
        await asyncio.sleep(0)
        return await original_fetch(message_id)

    channel.fetch_message = slow_fetch

    async def both():
        await asyncio.gather(
            items_bot.startraffle_cmd.callback(ctx),
            items_bot.startraffle_cmd.callback(ctx),
        )

    asyncio.run(both())

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is True
    assert raffle.eligible == ("Jjew",)
    assert len(items_bot._STATE.raffles) == 1


def test_two_officers_winning_the_same_raffle_tick_the_box_once(monkeypatch):
    """The session lock prevents concurrent `!won` calls from double-writing."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )

    writes = []

    def commit(spreadsheet, **kwargs):
        writes.append(kwargs["ign"])
        return "B2"

    monkeypatch.setattr(items_sheet, "commit_approval", commit)

    real_to_thread = asyncio.to_thread

    async def slow_to_thread(func, *args, **kwargs):
        await asyncio.sleep(0)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(items_bot.asyncio, "to_thread", slow_to_thread)

    async def both():
        await asyncio.gather(
            items_bot.won_cmd.callback(ctx, argument="Jjew"),
            items_bot.won_cmd.callback(ctx, argument="Jjew"),
        )

    asyncio.run(both())

    assert writes == ["Jjew"], f"checkbox written {len(writes)} times"
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winners == ("Jjew",)


def test_a_refused_poll_keeps_the_superseded_raffle_drawable(monkeypatch):
    """A failed replacement leaves the old frozen raffle usable by a session."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    channel = PollRejectingChannel(42)
    _register_channel(channel)
    _open_raffle(
        channel, ends="2020-01-01 00:00:00", eligible=("Jjew",), listed=True
    )
    ctx = FakeCtx(channel)
    ctx.author = FakeMember(roles=[FakeRole(10)])

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is not None

    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))
    assert "Jjew" in ctx.sent[-1]["embed"].description

    monkeypatch.setattr(items_sheet, "commit_approval", lambda spreadsheet, **kw: "B2")
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winners == ("Jjew",)


def test_no_command_is_defined_after_the_main_guard():
    """bot.run() blocks, so a command below the guard never registers.

    Importing items_bot cannot catch this: __name__ is not "__main__",
    so main() is skipped and every decorator runs. Only running the file
    as a script exposes it -- which is exactly what production does and
    the test suite does not. So the invariant is checked on the source.
    """
    import ast
    import pathlib

    source = pathlib.Path(items_bot.__file__).read_text()
    tree = ast.parse(source)

    guards = [
        node.lineno
        for node in tree.body
        if isinstance(node, ast.If)
        and ast.dump(node.test).find("__main__") != -1
    ]
    assert len(guards) == 1, f"expected exactly one __main__ guard, found {len(guards)}"

    late = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno > guards[0]
        and any(
            isinstance(d, ast.Call)
            and getattr(d.func, "attr", "") == "command"
            for d in node.decorator_list
        )
    ]
    assert late == [], f"these commands would never register in production: {late}"


def test_every_raffle_command_is_registered_on_the_bot():
    registered = {c.name for c in items_bot.bot.commands}
    for name in (
        "poll", "cancelpoll", "startraffle", "won", "skipraffle", "iam",
        "bind", "notaplayer", "setraffleroles", "setrafflechannel",
    ):
        assert name in registered, f"!{name} is not registered"


def test_startraffle_reads_the_poll_from_the_raffles_own_channel(monkeypatch):
    """An admin who moves the raffle channel mid-poll can still draw it."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    old_channel = _register_channel(FakeChannel(42))
    _, message = _open_raffle(old_channel, ends="2026-08-09 10:00:00")
    message.poll = FakePoll(answers=[FakePollAnswer("Yes", [FakeVoter(1, "BK | Jjew")])])

    new_channel = _register_channel(FakeChannel(77))
    items_bot._STATE.raffle_channel_id = 77
    ctx = FakeCtx(new_channel)
    ctx.author = FakeMember(roles=[FakeRole(10)])

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").eligible == ("Jjew",)


def test_startraffle_defers_to_discords_own_expiry_not_the_stored_one(monkeypatch):
    """ends_at is computed before the poll is posted, so it runs early.

    Freezing inside that gap would permanently exclude anyone who had
    not voted yet.
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel, ends="2026-08-09 10:00:00")
    poll = FakePoll(answers=[FakePollAnswer("Yes", [FakeVoter(1, "BK | Jjew")])])
    poll.expires_at = discord.utils.utcnow() + datetime.timedelta(minutes=5)
    message.poll = poll

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert ctx.sent[-1]["embed"].title == "❌ Poll still open"
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is False


def test_startraffle_lists_a_poll_discord_has_already_closed(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel, ends="2026-08-09 10:00:00")
    poll = FakePoll(answers=[FakePollAnswer("Yes", [FakeVoter(1, "BK | Jjew")])])
    poll.expires_at = discord.utils.utcnow() - datetime.timedelta(minutes=5)
    message.poll = poll

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").eligible == ("Jjew",)


def test_cancelpoll_deletes_a_poll_discord_has_already_closed(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel)
    poll = FakePoll()
    poll.expires_at = discord.utils.utcnow() - datetime.timedelta(minutes=5)
    message.poll = poll

    asyncio.run(items_bot.cancelpoll_cmd.callback(ctx, argument="Asta's Heart"))

    assert message.end_poll_calls == 0
    assert message.deleted
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart") is None


def test_the_pool_embed_stays_within_discords_description_limit():
    split = items_raffle.VoterSplit(
        eligible=[f"AnExtremelyLongPlayerName{n:04d}" for n in range(400)],
        already_have=[f"AnotherLongPlayerName{n:04d}" for n in range(200)],
        unidentified=[items_raffle.Voter(10**18 + n, "x") for n in range(400)],
    )

    rendered = items_bot.render_pool("Asta's Heart", split, winners=("Someone",))

    assert len(rendered) <= 4096, f"{len(rendered)} chars would be rejected by Discord"
    assert "Someone" in rendered, "the winner must never be truncated away"


def test_the_refusal_embed_stays_within_discords_description_limit(monkeypatch):
    """An embed Discord refuses names nobody -- the one thing this does."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(10**17 + n, f"stranger{n:03d}") for n in range(400)]),
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    description = ctx.sent[-1]["embed"].description
    assert len(description) <= 4096, f"{len(description)} chars would be rejected"
    assert "more" in description, "truncation must say how many were left out"
    assert "!iam" in description, "the instructions must survive truncation"
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is False


def test_redrawing_a_winner_whose_box_is_already_ticked_says_so(monkeypatch):
    """The state save can fail after the sheet write succeeded.

    After a restart the raffle looks open but the checkbox is ticked, so
    the officer retries `!won`. The sheet reports the box is already held, and a generic
    'nothing was recorded' would be actively wrong -- the item HAS been
    given. Close the raffle and say so instead.
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )

    def already_ticked(spreadsheet, **kwargs):
        raise items_sheet.AlreadyHeld(
            "Jjew already has \"Asta's Heart\" -- a special log is once only"
        )

    monkeypatch.setattr(items_sheet, "commit_approval", already_ticked)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew",), "the raffle must not stay open forever"
    assert "already" in ctx.sent[-2]["embed"].description.casefold()


def test_an_already_ticked_middle_name_does_not_stop_the_others(monkeypatch):
    """AlreadyHeld means the item HAS been given -- keep going."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00",
        eligible=("Jjew", "Kobe", "Ryuu"), listed=True,
    )
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    attempted = []

    def _kobe_already(spreadsheet, **kw):
        attempted.append(kw["ign"])
        if kw["ign"] == "Kobe":
            raise items_sheet.AlreadyHeld("Kobe already has it")
        return "C4"

    monkeypatch.setattr(items_sheet, "commit_approval", _kobe_already)

    asyncio.run(
        items_bot.won_cmd.callback(ctx, argument="Jjew - Kobe - Ryuu")
    )

    assert attempted == ["Jjew", "Kobe", "Ryuu"], "Ryuu must still be attempted"
    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew", "Kobe", "Ryuu")
    assert raffle.drawn is True
    assert "already" in ctx.sent[-2]["embed"].description.casefold()


def _state_fingerprint():
    """Everything about the raffle set that a rollback must preserve."""
    return sorted(r.to_dict().items().__str__() for r in items_bot._STATE.raffles)


def test_every_poll_failure_path_leaves_the_raffle_set_untouched(monkeypatch):
    """poll_cmd removes a superseded raffle and an eviction victim before
    it knows the poll will succeed. Each early return must put both back.
    """
    _configured_raffle(monkeypatch)
    _fill_every_raffle_slot(monkeypatch, ends="2026-08-09 12:00:00", already_drawn=("Log 0",))
    # Give one slot a superseded-able raffle for the item we will re-poll.
    items_bot._STATE.raffles[1] = items_state.Raffle(
        item="Asta's Heart", channel_id=42, message_id=999,
        created_at="2026-08-09 01:00:00", ends_at="2020-01-01 00:00:00",
        listed=True, eligible=("Jjew", "Kobe"),
    )
    _sheet(monkeypatch, special=("Player Name", "Asta's Heart",
                                 *[f"Log {n}" for n in range(items_state.MAX_RAFFLES)]))
    before = _state_fingerprint()

    # 1. Discord rejects the poll.
    channel = PollRejectingChannel(42)
    ctx = FakeCtx(channel)
    ctx.author = FakeMember(roles=[FakeRole(10)])
    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))
    assert _state_fingerprint() == before, "send failure lost a raffle"

    # 2. The resulting state would not fit in the pinned messages.
    monkeypatch.setattr(items_state, "fits", lambda state: False)
    ctx2, _ = _raffle_ctx()
    asyncio.run(items_bot.poll_cmd.callback(ctx2, argument="Asta's Heart"))
    assert _state_fingerprint() == before, "capacity refusal lost a raffle"
    monkeypatch.undo()


def test_a_corrupt_cell_is_not_mistaken_for_an_already_ticked_box(monkeypatch):
    """record_special quotes the offending cell into its message.

    A cell holding the words this handler once matched on would have
    closed the raffle and reported success while nothing was written.
    Only the dedicated AlreadyHeld type means "already ticked".
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )

    def corrupt_cell(spreadsheet, **kwargs):
        raise items_sheet.SheetStructureError(
            "Cell for Jjew / \"Asta's Heart\" holds 'a special log is once "
            "only', which is not a checkbox state; refusing to overwrite it"
        )

    monkeypatch.setattr(items_sheet, "commit_approval", corrupt_cell)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winners == ()
    assert ctx.sent[-1]["embed"].title == "❌ Nothing recorded"


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
    for name in (
        "poll", "cancelpoll", "startraffle", "won", "skipraffle", "iam",
        "bind", "notaplayer",
    ):
        assert items_bot.command_channels(FakeGuardCtx(30, name)) == (30,)


def test_set_channel_commands_stay_usable_anywhere(monkeypatch):
    monkeypatch.setattr(items_bot, "_STATE", _configured_state())
    for name in ("setofficerchannel", "setqueuechannel", "setrafflechannel"):
        ctx = FakeGuardCtx(999, name)
        assert items_bot.command_channels(ctx) is channel_guard.EXEMPT


def test_requests_are_unrestricted_until_a_queue_channel_is_set(monkeypatch):
    # Deploying before anyone runs !setqueuechannel must change nothing.
    monkeypatch.setattr(items_bot, "_STATE", items_state.State(officer_channel_id=10))
    ctx = FakeGuardCtx(999, "request")
    assert channel_guard.allows(999, items_bot.command_channels(ctx))


def test_every_registered_command_is_classified():
    # Guards against a future command silently defaulting to unguarded.
    for command in items_bot.bot.commands:
        assert command.name in items_bot._CLASSIFIED_COMMANDS, (
            f"!{command.name} is not listed in items_bot's channel map"
        )


def test_wrong_channel_is_swallowed_but_other_check_failures_still_reply():
    ctx = FakeCtx(FakeChannel(999))
    asyncio.run(
        items_bot.on_command_error(ctx, channel_guard.WrongChannel("nope"))
    )
    assert ctx.sent == []

    asyncio.run(items_bot.on_command_error(ctx, commands.CheckFailure("nope")))
    assert ctx.sent, "a plain CheckFailure must still be reported"


def test_a_failure_part_way_keeps_the_raffle_open_for_a_retry(monkeypatch):
    """The first name is already ticked; the rest must stay drawable."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00",
        eligible=("Jjew", "Kobe", "Ryuu"), listed=True,
    )
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )

    def _fail_on_kobe(spreadsheet, **kw):
        if kw["ign"] == "Kobe":
            raise RuntimeError("Sheets is down")
        return "C4"

    monkeypatch.setattr(items_sheet, "commit_approval", _fail_on_kobe)

    asyncio.run(
        items_bot.won_cmd.callback(ctx, argument="Jjew - Kobe - Ryuu")
    )

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew",)
    assert raffle.drawn is False
    description = ctx.sent[-1]["embed"].description
    assert "Sheets is down" in description
    assert "Ryuu" in description, "the un-attempted name must be named"
    assert "!won Kobe - Ryuu" in description
    assert ctx.sent[-1]["embed"].title == "❌ Partly recorded"


def test_won_refuses_with_no_session(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))

    assert "!startraffle" in ctx.sent[-1]["embed"].description


def test_skipraffle_leaves_the_log_undrawn_and_moves_on(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Kobe",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )

    asyncio.run(items_bot.skipraffle_cmd.callback(ctx))

    session = items_bot._STATE.raffle_session
    assert session.position == 1
    assert session.skipped == ("Log A",)
    assert items_state.find_raffle(items_bot._STATE, "Log A").drawn is False
    assert "Log B" in ctx.sent[-1]["embed"].title


def test_a_skipped_log_is_picked_up_by_the_next_session(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(items=("Log A",))

    asyncio.run(items_bot.skipraffle_cmd.callback(ctx))
    assert items_bot._STATE.raffle_session is None

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert items_bot._STATE.raffle_session.items == ("Log A",)


def test_skipraffle_refuses_with_no_session(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.skipraffle_cmd.callback(ctx))

    assert "!startraffle" in ctx.sent[-1]["embed"].description


def test_won_ticks_the_checkbox_and_advances_the_session(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    calls = {}
    monkeypatch.setattr(items_sheet, "commit_approval",
                        lambda s, **kw: calls.update(kw) or "C4")

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert calls["ign"] == "Kobe"
    assert calls["item"] == "Log A"
    session = items_bot._STATE.raffle_session
    assert session.position == 1
    assert session.results == (("Log A", ("Kobe",)),)
    assert items_state.find_raffle(items_bot._STATE, "Log A").drawn is True


def test_a_winner_is_excluded_from_every_later_pool(monkeypatch):
    """The whole point: one win per player per session."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    monkeypatch.setattr(items_sheet, "commit_approval", lambda s, **kw: "C4")

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    description = ctx.sent[-1]["embed"].description
    assert "Log B" in ctx.sent[-1]["embed"].title
    assert "1. Jjew" in description
    assert "Won earlier this session" in description
    # Kobe must not be offered again -- he appears only in the excluded group.
    assert "2. Kobe" not in description


def test_won_refuses_a_player_excluded_by_an_earlier_win(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=1,
        results=(("Log A", ("Kobe",)),),
    )
    monkeypatch.setattr(items_sheet, "commit_approval",
                        lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert "already won" in ctx.sent[-1]["embed"].description
    assert items_bot._STATE.raffle_session.position == 1


def test_won_refuses_a_player_not_in_the_pool(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(items=("Log A",))
    monkeypatch.setattr(items_sheet, "commit_approval",
                        lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert "not on the eligible list" in ctx.sent[-1]["embed"].description
    assert items_bot._STATE.raffle_session.position == 0


def test_won_records_several_winners_for_one_log(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(items=("Log A",))
    written = []
    monkeypatch.setattr(items_sheet, "commit_approval",
                        lambda s, **kw: written.append(kw["ign"]) or "C4")

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew - Kobe"))

    assert written == ["Jjew", "Kobe"]
    assert items_bot._STATE.raffle_session is None  # only poll, session ended


def test_a_failed_write_leaves_the_poll_current(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )

    def _boom(spreadsheet, **kwargs):
        raise RuntimeError("Google said no")

    monkeypatch.setattr(items_sheet, "commit_approval", _boom)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    session = items_bot._STATE.raffle_session
    assert session.position == 0, "a failed write must not move the session on"
    assert session.results == ()
    assert items_state.find_raffle(items_bot._STATE, "Log A").drawn is False


def test_a_failure_on_the_very_first_name_says_nothing_was_recorded(monkeypatch):
    """"Partly recorded" would imply the sheet holds something. It does not."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe"), listed=True
    )
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )

    def _boom(spreadsheet, **kw):
        raise RuntimeError("Sheets is down")

    monkeypatch.setattr(items_sheet, "commit_approval", _boom)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew - Kobe"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ()
    assert raffle.drawn is False
    assert ctx.sent[-1]["embed"].title == "❌ Nothing recorded"
    assert "Nothing was written to the sheet." in ctx.sent[-1]["embed"].description


def test_the_retry_after_a_partial_failure_completes_the_draw(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe", "Ryuu"),
        listed=True, winners=("Jjew",), drawn=False,
    )
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    monkeypatch.setattr(items_sheet, "commit_approval", lambda spreadsheet, **kw: "C4")

    asyncio.run(
        items_bot.won_cmd.callback(ctx, argument="Kobe - Ryuu")
    )

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew", "Kobe", "Ryuu")
    assert raffle.drawn is True


def test_retyping_an_already_recorded_winner_writes_nothing(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe", "Ryuu"),
        listed=True, winners=("Jjew",), drawn=False,
    )
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    monkeypatch.setattr(
        items_sheet, "commit_approval",
        lambda *a, **k: pytest.fail("wrote a second time"),
    )

    asyncio.run(
        items_bot.won_cmd.callback(ctx, argument="Jjew - Kobe")
    )

    description = ctx.sent[-1]["embed"].description
    assert "Jjew" in description
    assert "already" in description.casefold()
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winners == ("Jjew",)


def test_one_bad_name_refuses_the_whole_command(monkeypatch):
    """Nothing is ticked, so the officer can fix the typo and re-run as typed."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe"), listed=True
    )
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    monkeypatch.setattr(
        items_sheet, "commit_approval", lambda *a, **k: pytest.fail("wrote")
    )

    asyncio.run(
        items_bot.won_cmd.callback(ctx, argument="Jjew - Ryuu")
    )

    assert "not on the eligible list" in ctx.sent[-1]["embed"].description
    assert "Ryuu" in ctx.sent[-1]["embed"].description
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winners == ()


def test_a_ledger_failure_on_one_name_does_not_stop_the_others(monkeypatch):
    """Its checkbox IS ticked, so a retry would skip it and lose the row."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe"), listed=True
    )
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )
    row = ["2026-08-09 12:00:00", "Jjew", "Asta's Heart", "Special", "Keith", "1", "abc"]

    def _ledger_failure(spreadsheet, **kw):
        if kw["ign"] == "Jjew":
            raise items_sheet.LedgerWriteError("C4", row, RuntimeError("append failed"))
        return "C5"

    monkeypatch.setattr(items_sheet, "commit_approval", _ledger_failure)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew - Kobe"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew", "Kobe")
    assert raffle.drawn is True
    assert "C4" in ctx.sent[-2]["embed"].description


def test_render_pool_labels_several_winners():
    split = items_raffle.VoterSplit(eligible=["Jjew", "Kobe"])

    text = items_bot.render_pool("Asta's Heart", split, ("Jjew", "Kobe"))

    assert "Winners: Jjew, Kobe" in text
    assert "Winner: Jjew" in items_bot.render_pool("Asta's Heart", split, ("Jjew",))
    assert "Winner" not in items_bot.render_pool("Asta's Heart", split)


def test_won_freezes_the_next_poll_after_recording(monkeypatch):
    """The production path: the next poll has never been frozen.

    _record_winners runs holding _SHEET_LOCK and _post_current_poll takes
    it again through _freeze_raffle. asyncio.Lock is not reentrant, so
    calling the second inside the first deadlocks the bot while every
    already-listed test still passes. The timeout is what turns that
    deadlock into a failure instead of a hung suite.
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00")
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    monkeypatch.setattr(items_sheet, "commit_approval", lambda s, **kw: "C4")
    monkeypatch.setattr(
        items_bot, "poll_voters", _fake_poll_voters([(1, "Jjew"), (2, "Kobe")])
    )

    async def _run():
        await asyncio.wait_for(
            items_bot.won_cmd.callback(ctx, argument="Kobe"), timeout=5
        )

    asyncio.run(_run())

    log_b = items_state.find_raffle(items_bot._STATE, "Log B")
    assert log_b.listed is True, "the next poll should have been frozen"
    assert log_b.eligible == ("Jjew", "Kobe")
    description = ctx.sent[-1]["embed"].description
    assert "1. Jjew" in description
    assert "Won earlier this session" in description
    assert "2. Kobe" not in description


def _interleaving_to_thread(monkeypatch):
    """Force a suspension inside the sheet read so two commands overlap."""
    real_to_thread = asyncio.to_thread

    async def slow_to_thread(func, *args, **kwargs):
        await asyncio.sleep(0)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(items_bot.asyncio, "to_thread", slow_to_thread)


def test_two_officers_naming_different_winners_do_not_lose_one(monkeypatch):
    """The second !won must be refused, not overwrite the first.

    Both commands read the session before waiting on _SHEET_LOCK. If the
    loser of that race writes back a session derived from its stale
    snapshot, the first winner vanishes from session.winners -- and is
    eligible again in every later poll, which is the one thing this
    feature exists to prevent.
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    writes = []
    monkeypatch.setattr(
        items_sheet, "commit_approval",
        lambda s, **kw: writes.append((kw["item"], kw["ign"])) or "B2",
    )
    _interleaving_to_thread(monkeypatch)

    async def both():
        await asyncio.gather(
            items_bot.won_cmd.callback(ctx, argument="Kobe"),
            items_bot.won_cmd.callback(ctx, argument="Jjew"),
        )

    asyncio.run(both())

    assert len(writes) == 1, f"only one draw should have been written: {writes}"
    drawn_ign = writes[0][1]
    session = items_bot._STATE.raffle_session
    assert session.results == (("Log A", (drawn_ign,)),)
    assert session.winners == (drawn_ign,), "the recorded winner must survive"
    assert items_state.find_raffle(items_bot._STATE, "Log A").winners == (drawn_ign,)


def test_a_skip_racing_a_win_does_not_discard_the_win(monkeypatch):
    """!skipraffle must not write back a session snapshot taken before !won."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    monkeypatch.setattr(items_sheet, "commit_approval", lambda s, **kw: "B2")
    _interleaving_to_thread(monkeypatch)

    async def both():
        await asyncio.gather(
            items_bot.won_cmd.callback(ctx, argument="Kobe"),
            items_bot.skipraffle_cmd.callback(ctx),
        )

    asyncio.run(both())

    session = items_bot._STATE.raffle_session
    recorded = () if session is None else session.results
    assert ("Log A", ("Kobe",)) in recorded, (
        f"the recorded draw for Log A must survive the skip: {recorded}"
    )


def test_skipraffle_freezes_the_next_poll_without_deadlocking(monkeypatch):
    """!skipraffle holds _SHEET_LOCK; _post_current_poll takes it again.

    The timeout is what turns a reentrant-lock mistake into a failure
    rather than a bot that stops responding mid-session.
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00")
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    monkeypatch.setattr(
        items_bot, "poll_voters", _fake_poll_voters([(1, "Jjew"), (2, "Kobe")])
    )

    async def _run():
        await asyncio.wait_for(items_bot.skipraffle_cmd.callback(ctx), timeout=5)

    asyncio.run(_run())

    assert items_bot._STATE.raffle_session.skipped == ("Log A",)
    assert items_state.find_raffle(items_bot._STATE, "Log B").listed is True


def test_a_session_survives_a_restart_and_still_excludes_the_winner(monkeypatch):
    """The reason the session lives in the pin rather than in memory.

    Render's free tier restarts between commands. If the winner list did
    not come back, the player who won poll 1 would be drawable again in
    poll 2 and nothing would show that it happened.
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    monkeypatch.setattr(items_sheet, "commit_approval", lambda s, **kw: "B2")

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    # The restart: everything in memory is thrown away and rebuilt from
    # the pinned shards, exactly as load_state does on boot.
    reloaded = items_state.decode_shards(items_state.encode_state(items_bot._STATE))
    assert reloaded is not None
    items_bot._STATE = reloaded

    session = items_bot._STATE.raffle_session
    assert session is not None, "the session must come back from the pin"
    assert session.current_item == "Log B"
    assert session.winners == ("Kobe",)

    # And the exclusion still bites after the restart.
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert "already won earlier in this session" in ctx.sent[-1]["embed"].description
    assert items_state.find_raffle(items_bot._STATE, "Log B").winners == ()


def test_a_winner_from_a_failed_attempt_still_counts_against_the_session(monkeypatch):
    """A partial write leaves names recorded. They must still exclude.

    !won Kobe - Ryuu ticks Kobe, then Sheets fails on Ryuu. The officer
    re-runs !won Ryuu. If the session records only what the retry wrote,
    Kobe drops out of the winner list and can win a later poll -- having
    already been given a log in this same sitting.
    """
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe", "Ryuu"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe", "Ryuu"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )

    def commit(spreadsheet, **kwargs):
        if kwargs["ign"] == "Ryuu":
            raise RuntimeError("Sheets is down")
        return "B2"

    monkeypatch.setattr(items_sheet, "commit_approval", commit)
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe - Ryuu"))
    assert items_bot._STATE.raffle_session.position == 0, "the poll stays current"

    monkeypatch.setattr(items_sheet, "commit_approval", lambda s, **kw: "B2")
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Ryuu"))

    session = items_bot._STATE.raffle_session
    assert session.results == (("Log A", ("Kobe", "Ryuu")),)
    assert session.winners == ("Kobe", "Ryuu")
    # Log B's pool must offer neither of them.
    description = ctx.sent[-1]["embed"].description
    assert "1. Jjew" in description
    assert "Won earlier this session" in description
