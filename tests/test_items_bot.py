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
    outcome = _evaluate("Asta's Belt Kobe", state=state)
    assert not outcome.accepted


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


def test_pages_chunks_the_queue_so_every_request_is_reachable():
    queue = [_queued(f"id{n}", "Dajz", "Asta's Heart", items_rules.SPECIAL) for n in range(60)]
    chunks = items_bot.pages(queue)
    assert [len(c) for c in chunks] == [25, 25, 10]
    assert sum(len(c) for c in chunks) == 60


def test_pages_of_an_empty_queue_is_one_empty_page():
    assert items_bot.pages([]) == [[]]


def test_a_panel_remembers_which_page_it_shows():
    """Page 2 must redraw as page 2, not be replaced by page 1."""
    queue = [_queued(f"id{n}", "Dajz", "Asta's Heart", items_rules.SPECIAL) for n in range(30)]
    panel = items_bot.DistributePanel(items_bot.pages(queue)[1], start=26)
    assert panel.start == 26


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
