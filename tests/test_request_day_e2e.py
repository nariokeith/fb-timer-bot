"""Requesting on one day and being approved on the next, end to end.

The reported fault, played out a day at a time: a member asks for one
gear log in the evening, no officer gets to it, and it is approved the
following morning. The member then asks for their next one. With the
cap at 1 -- which is what the guild runs, set in the sheet's `_BotConfig`
tab -- that second request is the whole question: counting the approval
day charged it to the morning the officer clicked, so the member was
told they were at 1/1 on a day they had asked for nothing.

Only gspread is faked here, via the shared conftest fakes. Everything
between the member's words and the cells written is the shipped code:
read_snapshot (including reading the cap out of `_BotConfig`),
evaluate_request, pending_gear_for, approve's re-check inside the write
lock, and items_sheet.commit_approval writing a real ledger row into a
real grid. The Distribution Log deliberately starts with the SEVEN
column header a live sheet still has, so the widening runs here too.

save_state and refresh_board are stubbed: they are the Discord
transport, and test_items_bot covers them.
"""

import asyncio
import datetime

import pytest
from conftest import FakeSpreadsheet

import items_bot
import items_rules
import items_sheet
import items_state
from attendance_sheet import CONFIG_HEADER
from test_raffle_session_e2e import WritingWorksheet


MEMBER_ID = 4321
EVENING = datetime.datetime(2026, 9, 2, 21, 40, 11, tzinfo=items_rules.PHT)
NEXT_MORNING = datetime.datetime(2026, 9, 3, 9, 14, 2, tzinfo=items_rules.PHT)


def build_spreadsheet(cap="1"):
    """The Logs Tracker as it stands on the guild's live sheet."""
    return FakeSpreadsheet(
        {
            items_sheet.SPECIAL_TAB: WritingWorksheet(
                [["Player Name", "Asta's Heart"], ["Kobe", "FALSE"], ["Dajz", "FALSE"]],
                title=items_sheet.SPECIAL_TAB,
            ),
            items_sheet.GEAR_TAB: WritingWorksheet(
                [
                    ["Player Name", "Asta's Belt", "Benji's Heart"],
                    ["Kobe", "", ""],
                    ["Dajz", "", ""],
                ],
                title=items_sheet.GEAR_TAB,
            ),
            # Seven columns: the header a sheet deployed before request
            # times were recorded still has. The bot widens it itself.
            items_sheet.LEDGER_TAB: WritingWorksheet(
                [list(items_sheet.LEGACY_LEDGER_HEADER)],
                title=items_sheet.LEDGER_TAB,
                cols=7,
            ),
            items_sheet.CONFIG_TAB: WritingWorksheet(
                [list(CONFIG_HEADER), [items_sheet.GEAR_CAP_KEY, cap]],
                title=items_sheet.CONFIG_TAB,
            ),
        }
    )


@pytest.fixture(autouse=True)
def reset_module_state():
    """items_bot keeps _STATE and the remembered cap at module level.

    test_items_bot has its own copy of this; an autouse fixture only
    covers the module it is defined in, so without one here the second
    test in this file inherits the first one's queue.
    """
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []
    items_bot._SHEET_LOCK = asyncio.Lock()
    items_bot._LAST_KNOWN_GEAR_CAP = None
    yield
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []
    items_bot._SHEET_LOCK = asyncio.Lock()
    items_bot._LAST_KNOWN_GEAR_CAP = None


@pytest.fixture
def sheet(monkeypatch):
    spreadsheet = build_spreadsheet()
    monkeypatch.setattr(items_bot, "_SPREADSHEET", spreadsheet)

    async def noop_save(channel=None):
        return None

    async def noop_refresh():
        return None

    monkeypatch.setattr(items_bot, "save_state", noop_save)
    monkeypatch.setattr(items_bot, "refresh_board", noop_refresh)
    return spreadsheet


def at(monkeypatch, moment):
    """Freeze the bot's clock, the way a day passing would move it."""
    monkeypatch.setattr(items_rules, "now_pht", lambda: moment)
    monkeypatch.setattr(
        items_bot, "today_pht", lambda: items_rules.pht_day(
            items_rules.format_timestamp(moment)
        )
    )


def ask(spreadsheet, argument, day):
    """One `!request`, decided against a fresh read of the sheet."""
    snapshot = items_sheet.read_snapshot(spreadsheet)
    return items_bot.evaluate_request(
        argument,
        MEMBER_ID,
        snapshot,
        items_bot._STATE,
        cap=items_bot.gear_cap(snapshot),
        today=day,
    )


def test_the_cap_really_comes_out_of_the_sheet(sheet):
    """Everything below is only meaningful if the cap is 1."""
    assert items_bot.gear_cap(items_sheet.read_snapshot(sheet)) == 1


def test_a_request_approved_the_next_day_leaves_that_day_free(sheet, monkeypatch):
    # --- Day 1, evening: the member asks for one gear log. -----------
    at(monkeypatch, EVENING)
    first = ask(sheet, "Asta's Belt Kobe", "2026-09-02")
    assert first.accepted, first.message
    assert first.request.requested_at == "2026-09-02 21:40:11"
    items_bot._STATE.queue.append(first.request)

    # Still day 1: the cap of 1 bites immediately, on the queue alone.
    second = ask(sheet, "Benji's Heart Kobe", "2026-09-02")
    assert not second.accepted
    assert "1/1" in second.message

    # --- Day 2, morning: the officer approves last night's request. --
    at(monkeypatch, NEXT_MORNING)
    message = asyncio.run(items_bot.approve(first.request.id, "Nario"))
    assert "approved" in message.lower(), message
    assert items_bot._STATE.queue == []

    ledger = sheet.worksheet(items_sheet.LEDGER_TAB)
    assert ledger._rows[0] == items_sheet.LEDGER_HEADER, "the tab widened itself"
    written = ledger._rows[1]
    assert written[0] == "2026-09-03 09:14:02", "approved on day 2"
    assert written[7] == "2026-09-02 21:40:11", "but asked for on day 1"

    # --- Day 2: the member asks for their next one. ------------------
    # THE QUESTION. They have asked for nothing today, so it goes
    # through -- even though a row was written to the sheet minutes ago.
    third = ask(sheet, "Benji's Heart Kobe", "2026-09-03")
    assert third.accepted, f"refused on a day nothing was requested: {third.message}"
    items_bot._STATE.queue.append(third.request)

    # And day 2 is now spent: one per day, still one per day.
    fourth = ask(sheet, "Asta's Belt Kobe", "2026-09-03")
    assert not fourth.accepted
    assert "1/1" in fourth.message


def test_the_leftover_request_still_cannot_be_approved_twice_over(sheet, monkeypatch):
    """The other direction: two requests from day 1 are still capped at 1.

    Backdating must not become a way to hand out more than the rule
    allows -- it only puts each item on the day it was asked for.
    """
    at(monkeypatch, EVENING)
    first = ask(sheet, "Asta's Belt Kobe", "2026-09-02")
    items_bot._STATE.queue.append(first.request)
    # A second day-1 request that only exists because an officer added
    # it by hand, or because the queue was restored from an old shard.
    items_bot._STATE.queue.append(
        items_state.PendingRequest(
            id="leftover", user_id=MEMBER_ID, ign="Kobe", item="Benji's Heart",
            type=items_rules.GEAR, requested_at="2026-09-02 22:10:00",
        )
    )

    at(monkeypatch, NEXT_MORNING)
    assert "approved" in asyncio.run(
        items_bot.approve(first.request.id, "Nario")
    ).lower()
    refused = asyncio.run(items_bot.approve("leftover", "Nario"))

    assert "not approved" in refused.lower(), refused
    assert "1/1" in refused
    assert len(items_bot._STATE.queue) == 1, "a refused request stays queued"
    assert len(sheet.worksheet(items_sheet.LEDGER_TAB)._rows) == 2, "one row only"
