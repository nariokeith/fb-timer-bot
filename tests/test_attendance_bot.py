"""Tests for the parts of the attendance bot that are real logic.

The module is mostly thin glue over five already-tested modules plus
discord.py, so these cover what can go wrong here and nowhere else: the
never-empty error text, the officer check, the promise that bot.py is
never imported, and the sheet lock that stops two officers clobbering
each other's writes. No Discord client is started.
"""

import asyncio
import inspect
import re
import sys

import attendance_bot
from attendance_bot import _is_officer, error_text


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeMember:
    def __init__(self, roles=()):
        self.roles = list(roles)


# --------------------------------------------------------------------------
# error_text: an embed with a blank body tells the officer nothing at all
# --------------------------------------------------------------------------


def test_bare_permission_error_still_produces_a_message():
    # gspread re-raises a permissions APIError as a bare PermissionError,
    # and str(PermissionError()) is "".
    assert str(PermissionError()) == ""
    assert error_text(PermissionError()).strip()


def test_empty_message_falls_back_to_the_cause():
    try:
        try:
            raise RuntimeError("the real diagnostic: 403 caller has no access")
        except RuntimeError as cause:
            raise PermissionError() from cause
    except PermissionError as exc:
        text = error_text(exc)

    assert "403 caller has no access" in text


def test_normal_exception_keeps_its_own_message():
    assert error_text(ValueError("No column for 'Lucus'")) == "No column for 'Lucus'"


def test_empty_exception_with_no_cause_falls_back_to_repr():
    text = error_text(PermissionError())
    assert text.strip()
    assert "None" not in text  # never renders str(None) from a missing __cause__


def test_whitespace_only_message_is_treated_as_empty():
    exc = RuntimeError("   ")
    assert error_text(exc).strip()


# --------------------------------------------------------------------------
# _is_officer
# --------------------------------------------------------------------------


def test_member_with_no_roles_is_not_an_officer():
    assert _is_officer(FakeMember(), 123) is False


def test_wrong_role_id_is_not_an_officer():
    assert _is_officer(FakeMember([FakeRole(999)]), 123) is False


def test_right_role_id_is_an_officer():
    assert _is_officer(FakeMember([FakeRole(999), FakeRole(123)]), 123) is True


def test_unconfigured_role_id_denies_everyone():
    # None means !setofficerrole has never been run; nobody may log.
    assert _is_officer(FakeMember([FakeRole(123)]), None) is False


# --------------------------------------------------------------------------
# The timer must stay untouched
# --------------------------------------------------------------------------


def test_importing_the_attendance_bot_does_not_import_the_timer():
    assert "bot" not in sys.modules


def test_all_five_commands_are_registered():
    names = {command.name for command in attendance_bot.bot.commands}
    assert {
        "attendance",
        "undoattendance",
        "setweek",
        "setofficerrole",
        "attendancehelp",
    } <= names


# --------------------------------------------------------------------------
# The sheet lock. attendance_sheet and its undo pair both document that
# serialising read-modify-write is the command layer's job; these assert
# this module actually does it.
# --------------------------------------------------------------------------


def test_a_module_level_lock_exists():
    assert isinstance(attendance_bot._SHEET_LOCK, asyncio.Lock)


def test_the_lock_helper_holds_the_lock_around_the_blocking_call():
    source = inspect.getsource(attendance_bot._locked)
    assert "async with _SHEET_LOCK:" in source
    assert "asyncio.to_thread" in source


def _call_sites(name: str) -> list[str]:
    """Lines that call `name`, excluding its own def and any docstring."""
    source = inspect.getsource(attendance_bot)
    pattern = re.compile(rf"(?<![\w.]){re.escape(name)}\s*\(")
    return [
        line.strip()
        for line in source.splitlines()
        if pattern.search(line) and not line.lstrip().startswith("def ")
    ]


def test_the_sheet_mutations_only_happen_inside_the_locked_helpers():
    # plan_point_writes/apply_writes must not be reachable from anywhere
    # except _commit and _reverse_last, which are only ever run via _locked.
    allowed = inspect.getsource(attendance_bot._commit) + inspect.getsource(
        attendance_bot._reverse_last
    )
    for name in ("plan_point_writes", "apply_writes", "mark_entry_reversed"):
        sites = _call_sites(name)
        assert sites, f"{name} is not called anywhere; test is stale"
        for site in sites:
            assert site in allowed, f"{name} called outside the locked helpers: {site}"


def _statements() -> list[str]:
    """Module source as logical statements, comments stripped.

    Physical lines are joined while brackets are unbalanced, so a call
    split across several lines is scanned as the single statement it is.
    """
    source = inspect.getsource(attendance_bot)
    statements, buffer, depth = [], "", 0
    for line in source.splitlines():
        code = line.split("#")[0].rstrip()
        if not code.strip():
            continue
        buffer = f"{buffer} {code.strip()}" if buffer else code.strip()
        depth += code.count("(") - code.count(")")
        if depth <= 0:
            statements.append(" ".join(buffer.split()))
            buffer, depth = "", 0
    if buffer:
        statements.append(" ".join(buffer.split()))
    return statements


def _reference_lines(name: str) -> list[str]:
    """Statements mentioning `name`, excluding its own definition."""
    pattern = re.compile(rf"(?<![\w.]){re.escape(name)}(?![\w])")
    return [
        statement
        for statement in _statements()
        if pattern.search(statement)
        and not statement.startswith(("def ", "async def ", "class "))
    ]


def test_every_mutating_helper_is_invoked_only_through_locked():
    for name in ("_commit", "_reverse_last", "_set_target_tab", "_set_officer_role"):
        sites = _reference_lines(name)
        assert sites, f"{name} is never invoked; test is stale"
        for site in sites:
            assert "_locked(" in site, f"{name} invoked outside _locked: {site}"


def test_the_lock_is_not_held_while_waiting_for_the_officers_reaction():
    # Holding it across wait_for would stall every other command for up to
    # PREVIEW_TIMEOUT seconds, so the command body must never take the lock
    # itself -- only _locked does, around the blocking call alone.
    source = inspect.getsource(attendance_bot.attendance_cmd.callback)
    assert "bot.wait_for(" in source
    assert "async with" not in source


# --------------------------------------------------------------------------
# Partial failure. Both sheet mutations are two-stage, and the second stage
# can fail after the first has already landed. What the officer is told
# after that decides whether they re-run and double-pay.
# --------------------------------------------------------------------------


import pytest

from attendance_sheet import LOG_HEADER, SheetStructureError
from conftest import SAMPLE_GRID, FakeSpreadsheet, FakeWorksheet

TAB = "Week 17"
ATTACHMENT = "1234567890"


def _entry(**overrides):
    entry = {
        "timestamp": "2026-08-06T20:00:00+08:00",
        "tab": TAB,
        "boss": "Lucus",
        "points_each": 3,
        "message_id": "999",
        "attachment_id": ATTACHMENT,
        "confirmed_by": "Officer#1",
        "players": "Kobe",
        "reversed": "",
    }
    entry.update(overrides)
    return entry


def _log_tab(*rows):
    return FakeWorksheet([list(LOG_HEADER), *rows], title="_BotLog")


def _sheets(grid=None, log=None):
    sheets = {TAB: FakeWorksheet(grid or SAMPLE_GRID, title=TAB)}
    if log is not None:
        sheets["_BotLog"] = log
    return FakeSpreadsheet(sheets)


@pytest.fixture
def spreadsheet(monkeypatch):
    """Install a fake spreadsheet and hand it back for assertions."""

    def install(sh):
        monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
        return sh

    return install


# (aa) points written, log row not


def test_commit_that_fails_after_writing_points_says_so(spreadsheet, monkeypatch):
    sh = spreadsheet(_sheets())

    def boom(*args, **kwargs):
        raise SheetStructureError("row 1 is not what this code expects")

    monkeypatch.setattr(attendance_bot, "append_log_entry", boom)

    with pytest.raises(attendance_bot.PointsWrittenButNotLogged) as caught:
        attendance_bot._commit(TAB, "Lucus", ["Kobe"], 3, _entry(), False)

    # The points really did land, so the officer must not be told otherwise.
    assert sh.worksheet(TAB).batches, "points should already be in the sheet"

    description = caught.value.description
    assert "Nothing was written" not in description
    assert "were added" in description
    assert "not re-run" in description.replace("NOT", "not")
    for detail in ("Lucus", "3", "Kobe", TAB):
        assert detail in description


def test_commit_that_fails_while_writing_points_writes_nothing(
    spreadsheet, monkeypatch
):
    sh = spreadsheet(_sheets())
    monkeypatch.setattr(
        attendance_bot,
        "apply_writes",
        lambda *a, **k: (_ for _ in ()).throw(SheetStructureError("no")),
    )

    with pytest.raises(SheetStructureError):
        attendance_bot._commit(TAB, "Lucus", ["Kobe"], 3, _entry(), False)

    assert not sh.worksheet(TAB).batches
    assert not isinstance(
        SheetStructureError("no"), attendance_bot.PointsWrittenButNotLogged
    )


# (bb) points removed, entry not marked reversed


def test_undo_that_fails_after_subtracting_says_the_points_are_already_gone(
    spreadsheet, monkeypatch
):
    row = [str(_entry()[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(log=_log_tab(row)))

    def boom(*args, **kwargs):
        raise SheetStructureError("update_cell failed")

    monkeypatch.setattr(attendance_bot, "mark_entry_reversed", boom)

    with pytest.raises(attendance_bot.PointsRemovedButNotMarked) as caught:
        attendance_bot._reverse_last()

    assert sh.worksheet(TAB).batches, "points should already be subtracted"

    description = caught.value.description
    assert "already" in description
    assert "twice" in description
    assert "not re-run" in description.replace("NOT", "not")
    assert "Lucus" in description


# (cc) the column must be re-resolved by boss name at commit time


def test_commit_follows_the_boss_name_when_the_column_moves(spreadsheet):
    # Preview resolved "Lucus" to column 3 (C). Between preview and commit
    # someone inserts a column, so Lucus is now column 4 (D) and column 3
    # holds a different boss entirely.
    shifted = [
        ["Player Name", "Points", "Nevaeh - 3", "Lucus - 3", "EGO", "Livera"],
        ["ARCILynN", "51", "", "", "1", "3"],
        ["xSigarilyas", "49", "", "3", "2", "3"],
        ["Kobe", "44", "", "", "1", "3"],
    ]
    assert SAMPLE_GRID[0][2].startswith("Lucus")  # was column 3 at preview time

    sh = spreadsheet(_sheets(grid=shifted))
    attendance_bot._commit(TAB, "Lucus", ["Kobe"], 3, _entry(), False)

    ranges = [cell["range"] for batch in sh.worksheet(TAB).batches for cell in batch]
    assert ranges == ["D4"], f"write followed a stale index instead of the name: {ranges}"


def test_commit_takes_a_boss_name_not_a_column_index():
    parameters = inspect.signature(attendance_bot._commit).parameters
    assert "boss" in parameters
    assert "column" not in parameters
    assert "find_column" in inspect.getsource(attendance_bot._commit)


def test_commit_refuses_when_the_boss_column_vanished_mid_preview(spreadsheet):
    without_lucus = [
        ["Player Name", "Points", "EGO", "Livera"],
        ["Kobe", "44", "1", "3"],
    ]
    sh = spreadsheet(_sheets(grid=without_lucus))

    with pytest.raises(SheetStructureError):
        attendance_bot._commit(TAB, "Lucus", ["Kobe"], 3, _entry(), False)

    assert not sh.worksheet(TAB).batches


# duplicate confirmed by someone else during the 180s preview window


def test_commit_aborts_if_the_screenshot_was_logged_during_the_preview(spreadsheet):
    row = [str(_entry()[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(log=_log_tab(row)))

    # was_duplicate=False: the preview showed no warning, so this row
    # appeared while the officer was deciding.
    with pytest.raises(attendance_bot.AlreadyLoggedDuringPreview):
        attendance_bot._commit(TAB, "Lucus", ["Kobe"], 3, _entry(), False)

    assert not sh.worksheet(TAB).batches


def test_commit_allows_a_duplicate_the_officer_was_warned_about(spreadsheet):
    row = [str(_entry()[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(log=_log_tab(row)))

    # was_duplicate=True: the preview carried the "Already Logged" warning
    # and the officer confirmed anyway, which is a legitimate re-log.
    attendance_bot._commit(TAB, "Lucus", ["Kobe"], 3, _entry(), True)

    assert sh.worksheet(TAB).batches


# --------------------------------------------------------------------------
# (z) and the minors
# --------------------------------------------------------------------------


def test_the_builtin_help_command_is_disabled():
    # "!help" is the only command name that collides with the production
    # timer; leaving it enabled makes both bots answer.
    assert attendance_bot.bot.help_command is None
    assert "help" not in {command.name for command in attendance_bot.bot.commands}


def test_attendance_consumes_the_rest_of_the_line_as_the_boss_name():
    # "!attendance Lady Dalia" must not silently become "Lady".
    parameter = inspect.signature(
        attendance_bot.attendance_cmd.callback
    ).parameters["boss_name"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_log_timestamp_is_manila_local_time():
    stamp = attendance_bot._timestamp()
    assert stamp.endswith("+08:00")
    # ISO, second resolution, as append_log_entry stores it.
    from datetime import datetime

    assert datetime.fromisoformat(stamp).microsecond == 0


# --------------------------------------------------------------------------
# (dd) The do-not-re-run message must always render. Discord caps an embed
# description at 4096 characters; if these overflow, working.edit raises and
# the officer sees a generic error instead of the warning that stops them
# re-running a command whose points already landed.
# --------------------------------------------------------------------------

DISCORD_EMBED_DESCRIPTION_LIMIT = 4096

# The guild's real sheet has 49 players.
BIG_ROSTER = [f"PlayerNumber{n:02d}WithALongName" for n in range(49)]
BIG_CAUSE = "gspread said: " + ("x" * 5000)

PARTLY_WRITTEN_SENTENCES = ("were added", "not re-run", "undoattendance")
PARTLY_UNDONE_SENTENCES = ("already", "not re-run", "twice")


def _big_written():
    return attendance_bot.PointsWrittenButNotLogged(
        tab="Week 17.1",
        boss="Lucus",
        points=3,
        players=BIG_ROSTER,
        cause_text=BIG_CAUSE,
    )


def _big_removed():
    return attendance_bot.PointsRemovedButNotMarked(
        entry={
            "tab": "Week 17.1",
            "boss": "Lucus",
            "points_each": "3",
            "players": ", ".join(BIG_ROSTER),
        },
        cause_text=BIG_CAUSE,
    )


def test_partly_written_description_fits_in_an_embed():
    description = _big_written().description
    assert len(description) <= DISCORD_EMBED_DESCRIPTION_LIMIT


def test_partly_written_keeps_every_instruction_under_clamping():
    # The fixed instructional text carries the whole meaning; it must
    # survive intact no matter how long the variable parts are.
    description = _big_written().description
    for sentence in PARTLY_WRITTEN_SENTENCES:
        assert sentence in description
    assert "Lucus" in description
    assert "Week 17.1" in description


def test_partly_undone_description_fits_in_an_embed():
    description = _big_removed().description
    assert len(description) <= DISCORD_EMBED_DESCRIPTION_LIMIT


def test_partly_undone_keeps_every_instruction_under_clamping():
    description = _big_removed().description
    for sentence in PARTLY_UNDONE_SENTENCES:
        assert sentence in description
    assert "Lucus" in description


def test_the_real_rosters_player_list_is_not_clamped():
    # 49 players is what the guild's sheet actually holds; only the cause
    # text drives the overflow at that size, so the names all survive.
    description = _big_written().description
    assert "more" not in description
    for player in BIG_ROSTER:
        assert player in description


# Bigger than any real roster, to exercise the clamp itself.
HUGE_ROSTER = [f"PlayerNumber{n:03d}WithAVeryLongName" for n in range(400)]


def test_a_clamped_player_list_says_it_is_partial():
    # Otherwise the officer takes the visible names for the whole list.
    written = attendance_bot.PointsWrittenButNotLogged(
        tab="Week 17.1", boss="Lucus", points=3,
        players=HUGE_ROSTER, cause_text=BIG_CAUSE,
    )
    removed = attendance_bot.PointsRemovedButNotMarked(
        entry={
            "tab": "Week 17.1", "boss": "Lucus", "points_each": "3",
            "players": ", ".join(HUGE_ROSTER),
        },
        cause_text=BIG_CAUSE,
    )

    for exc in (written, removed):
        description = exc.description
        assert len(description) <= DISCORD_EMBED_DESCRIPTION_LIMIT
        count = int(re.search(r"and (\d+) more", description).group(1))
        assert 0 < count < len(HUGE_ROSTER)
        # The instructions still survive alongside the clamped list.
        assert "not re-run" in description


def test_a_small_case_is_untouched():
    exc = attendance_bot.PointsWrittenButNotLogged(
        tab="Week 17",
        boss="Lucus",
        points=3,
        players=["Kobe", "ARCILynN"],
        cause_text="row 1 is not what this code expects",
    )
    description = exc.description

    assert "more" not in description  # no truncation indicator
    assert "…" not in description
    assert "Kobe, ARCILynN" in description
    assert "row 1 is not what this code expects" in description


def test_a_small_undo_case_is_untouched():
    exc = attendance_bot.PointsRemovedButNotMarked(
        entry={
            "tab": "Week 17",
            "boss": "Lucus",
            "points_each": "3",
            "players": "Kobe, ARCILynN",
        },
        cause_text="update_cell failed",
    )
    description = exc.description

    assert "more" not in description
    assert "…" not in description
    assert "Kobe, ARCILynN" in description
    assert "update_cell failed" in description
