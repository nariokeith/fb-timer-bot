"""Tests for the item bot's Discord wiring.

No Discord client is started and nothing touches the network; the fakes
below stand in for the handful of discord.py objects the handlers use,
following the local-fakes style of test_attendance_bot.py.
"""

import asyncio

import discord
import pytest

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
    ):
        self.content = content
        self.id = message_id
        self.pinned = False
        self.deleted = False
        self.edits: list[str] = []
        self.edit_calls = 0
        self.raise_on_edit = raise_on_edit
        self.raise_on_delete = raise_on_delete
        self.embed = None
        self.view = None

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
        self.pinned = True

    async def delete(self):
        if self.raise_on_delete:
            raise _http_exception()
        self.deleted = True


def _http_exception():
    response = type("Response", (), {"status": 500, "reason": "Server Error"})()
    return discord.HTTPException(response, "Discord failed")


class FakeChannel:
    def __init__(self, channel_id=99, pins=None, history=None):
        self.id = channel_id
        self.mention = f"#channel-{channel_id}"
        self._pins = list(pins or [])
        self._history = list(history or [])
        self.sent: list[FakeMessage] = []

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

    async def send(self, content=None, **kwargs):
        message = FakeMessage(content=content or "", message_id=len(self.sent) + 1)
        message.embed = kwargs.get("embed")
        message.view = kwargs.get("view")
        self.sent.append(message)
        self._pins.append(message)
        return message


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
    yield
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []


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
    assert all(message.edits for message in items_bot._STATE_MESSAGES)


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

    asyncio.run(items_bot.request_cmd.callback(ctx, argument="Asta's Heart Dajz"))

    assert items_bot._STATE.queue == []
    assert items_bot._STATE.igns == {"1": "Kobe"}
    assert "unreachable" in ctx.sent[-1]["embed"].title.lower()


def test_request_that_would_exceed_state_capacity_is_refused_without_changes(monkeypatch):
    items_bot._STATE.officer_channel_id = 99
    items_bot._STATE.queue = [
        _queued(f"id{n:03d}", f"Player {n}", "Asta's Heart", items_rules.SPECIAL)
        for n in range(140)
    ]
    items_bot._STATE.igns = {"1": "Kobe"}
    before_queue = list(items_bot._STATE.queue)
    before_igns = dict(items_bot._STATE.igns)
    ctx = FakeCtx(FakeChannel(1))
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)

    asyncio.run(items_bot.request_cmd.callback(ctx, argument="Asta's Heart Dajz"))

    assert items_bot._STATE.queue == before_queue
    assert items_bot._STATE.igns == before_igns
    assert "queue is full" in ctx.sent[-1]["embed"].title.lower()


import items_rules
import items_sheet

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


def test_a_valid_special_request_is_accepted():
    outcome = _evaluate("Asta's Heart Dajz")
    assert outcome.accepted
    assert outcome.request.item == "Asta's Heart"
    assert outcome.request.type == items_rules.SPECIAL


def test_a_multi_word_ign_is_accepted():
    outcome = _evaluate("Asta's Heart chinchong ni Mumu")
    assert outcome.accepted
    assert outcome.request.ign == "chinchong ni Mumu"


def test_a_special_the_player_already_holds_is_refused():
    outcome = _evaluate("Asta's Heart Kobe")
    assert not outcome.accepted
    assert "already" in outcome.message.lower()


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
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: calls.append(k))

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert calls == []
    assert items_bot._STATE.queue == []
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
                id="x", user_id=1, ign="Dajz", item="Asta's Heart",
                type=items_rules.SPECIAL, requested_at="2026-08-07 10:30:00",
            )
        ]
    )
    outcome = _evaluate("Asta's Heart Dajz", state=state)
    assert not outcome.accepted
    assert "pending" in outcome.message.lower()


def test_an_unparseable_request_is_refused_with_the_reason():
    outcome = _evaluate("Asta's Heart Nobody")
    assert not outcome.accepted
    assert "Nobody" in outcome.message


def test_an_ign_differing_from_last_time_is_noted_not_refused():
    """Requesting for an alt is legitimate; the officer judges it."""
    state = items_state.State(igns={"1": "Kobe"})
    outcome = _evaluate("Asta's Heart Dajz", state=state, user_id=1)
    assert outcome.accepted
    assert "Kobe" in outcome.request.note


def test_the_same_ign_as_last_time_carries_no_note():
    state = items_state.State(igns={"1": "Dajz"})
    outcome = _evaluate("Asta's Heart Dajz", state=state, user_id=1)
    assert outcome.accepted
    assert outcome.request.note == ""


def test_a_duplicate_is_refused_even_from_a_different_account():
    """Keyed on IGN, not on who asked."""
    state = items_state.State(
        queue=[
            items_state.PendingRequest(
                id="x", user_id=999, ign="Dajz", item="Asta's Heart",
                type=items_rules.SPECIAL, requested_at="2026-08-07 10:30:00",
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
