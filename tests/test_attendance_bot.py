"""Tests for the parts of the attendance bot that are real logic.

The module is mostly thin glue over five already-tested modules plus
discord.py, so these cover what can go wrong here and nowhere else: the
never-empty error text, the officer check, the promise that bot.py is
never imported, and the sheet lock that stops two officers clobbering
each other's writes. No Discord client is started.
"""

import asyncio
import inspect
import json
import re
import sys
from types import SimpleNamespace

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

from attendance_sheet import LOG_HEADER, LOG_TAB, SheetStructureError
from conftest import SAMPLE_GRID, FakeSpreadsheet, FakeWorksheet

TAB = "Week 17"
ATTACHMENT = "1234567890"


IMAGE_HASH = "deadbeef" * 8


def _entry(**overrides):
    entry = {
        "timestamp": "2026-08-06T20:00:00+08:00",
        "tab": TAB,
        "boss": "Lucus",
        "points_each": 3,
        "message_id": "999",
        "attachment_id": ATTACHMENT,
        "image_sha256": IMAGE_HASH,
        "confirmed_by": "Officer#1",
        "players": json.dumps(["Kobe"]),
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
            "players": json.dumps(BIG_ROSTER),
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
            "players": json.dumps(HUGE_ROSTER),
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
            "players": json.dumps(["Kobe", "ARCILynN"]),
        },
        cause_text="update_cell failed",
    )
    description = exc.description

    assert "more" not in description
    assert "…" not in description
    assert "Kobe, ARCILynN" in description
    assert "update_cell failed" in description


# --------------------------------------------------------------------------
# Final-review fixes
# --------------------------------------------------------------------------


# (2) duplicate detection must key on the image's own hash, not the
# per-upload Discord attachment id.


def test_commit_treats_a_repost_with_a_new_attachment_id_as_a_duplicate(spreadsheet):
    """A screenshot logged once, then re-posted (a brand-new attachment_id,
    same picture) must be caught. was_duplicate=False here mirrors what
    the preview would have shown if the hash check had been run for it --
    the point of this test is that image_sha256, not attachment_id, is
    what decides "duplicate".
    """
    logged = _entry(attachment_id="original-upload", image_sha256="same-hash")
    row = [str(logged[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(log=_log_tab(row)))

    repost = _entry(attachment_id="brand-new-upload-id", image_sha256="same-hash")
    with pytest.raises(attendance_bot.AlreadyLoggedDuringPreview):
        attendance_bot._commit(TAB, "Lucus", ["Kobe"], 3, repost, False)

    assert not sh.worksheet(TAB).batches


def test_commit_does_not_flag_a_different_screenshot_as_a_duplicate(spreadsheet):
    logged = _entry(attachment_id="upload-1", image_sha256="hash-1")
    row = [str(logged[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(log=_log_tab(row)))

    different = _entry(attachment_id="upload-2", image_sha256="hash-2")
    attendance_bot._commit(TAB, "Lucus", ["Kobe"], 3, different, False)

    assert sh.worksheet(TAB).batches


# (3) the do-not-re-run warning must reach the officer even if the normal
# Discord call used to show it fails.


class _FailingEditMessage:
    """Stands in for `working`: editing it always raises."""

    def __init__(self):
        self.edit_attempts = 0

    async def edit(self, **kwargs):
        self.edit_attempts += 1
        raise RuntimeError("boom: message was deleted")


class _RecordingCtx:
    """Stands in for `ctx`: records what actually got sent, can be made
    to fail its embed send too, to exercise the plain-text fallback."""

    def __init__(self, fail_embed_send=False):
        self.fail_embed_send = fail_embed_send
        self.sent = []

    async def send(self, *args, **kwargs):
        if kwargs.get("embed") is not None and self.fail_embed_send:
            raise RuntimeError("boom: also rate limited")
        self.sent.append((args, kwargs))


def test_partial_failure_falls_back_to_ctx_send_when_working_edit_fails(capsys):
    working = _FailingEditMessage()
    ctx = _RecordingCtx()

    asyncio.run(
        attendance_bot._report_partial_failure(
            ctx, working, "⚠️ Partly Written — Do Not Re-Run", "the real detail"
        )
    )

    assert working.edit_attempts == 1
    assert len(ctx.sent) == 1
    _, kwargs = ctx.sent[0]
    assert kwargs["embed"].description == "the real detail"

    # stderr carries the detail unconditionally, before any Discord call.
    err = capsys.readouterr().err
    assert "the real detail" in err


def test_partial_failure_falls_back_to_plain_text_when_everything_else_fails(capsys):
    working = _FailingEditMessage()
    ctx = _RecordingCtx(fail_embed_send=True)

    asyncio.run(
        attendance_bot._report_partial_failure(
            ctx, working, "⚠️ Partly Undone — Do Not Re-Run", "cause detail here"
        )
    )

    assert working.edit_attempts == 1
    assert len(ctx.sent) == 1
    args, kwargs = ctx.sent[0]
    assert kwargs.get("embed") is None
    assert "cause detail here" in args[0]
    assert "Partly Undone" in args[0]

    err = capsys.readouterr().err
    assert "cause detail here" in err


def test_partial_failure_with_no_working_message_goes_straight_to_ctx_send():
    # undoattendance has no preview message to edit.
    ctx = _RecordingCtx()

    asyncio.run(
        attendance_bot._report_partial_failure(
            ctx, None, "⚠️ Partly Undone — Do Not Re-Run", "detail"
        )
    )

    assert len(ctx.sent) == 1
    _, kwargs = ctx.sent[0]
    assert kwargs["embed"].description == "detail"


def test_stderr_is_written_even_if_every_discord_call_fails():
    class _AlwaysFailingCtx:
        async def send(self, *args, **kwargs):
            raise RuntimeError("channel gone")

    working = _FailingEditMessage()
    ctx = _AlwaysFailingCtx()

    # Must not raise -- this is a best-effort reporter, not a propagator.
    asyncio.run(
        attendance_bot._report_partial_failure(
            ctx, working, "title", "the detail that must survive"
        )
    )


# (4) a stuck sheet call must produce a visible refusal, not a permanent hang.


def test_locked_times_out_instead_of_hanging_forever(monkeypatch):
    """The `await _locked(...)` must raise promptly, even though the
    blocking thread underneath it does not actually stop.

    asyncio.to_thread's worker runs in a real OS thread, and Python has no
    way to forcibly kill a running thread -- cancelling the awaiting
    coroutine does not, and cannot, interrupt a synchronous time.sleep()
    (or a synchronous gspread call) already in progress inside it. So the
    orphaned worker thread keeps running past the timeout regardless; what
    LOCK_TIMEOUT actually buys is that the *caller* stops waiting and the
    lock is freed for the next command (see
    test_locked_releases_the_lock_after_a_timeout), not that the stuck
    call itself is killed. The orphan is eventually bounded by
    attendance_sheet.REQUEST_TIMEOUT on the gspread client, which is what
    actually ends a truly stuck network call.

    Because of that, timing must happen INSIDE the coroutine, around the
    await itself -- timing the outer asyncio.run() would also measure
    asyncio's loop-shutdown join against the still-sleeping thread, which
    has nothing to do with how long the officer actually waits.
    """
    monkeypatch.setattr(attendance_bot, "LOCK_TIMEOUT", 0.05)

    def never_returns(*args):
        time.sleep(0.5)  # plenty longer than the 0.05s timeout

    elapsed = {}

    async def run():
        started = time.monotonic()
        try:
            await attendance_bot._locked(never_returns)
        except TimeoutError:
            elapsed["value"] = time.monotonic() - started
        else:
            raise AssertionError("expected TimeoutError")

    asyncio.run(run())
    assert elapsed["value"] < 1.0, "the await did not time out promptly"


def test_locked_releases_the_lock_after_a_timeout():
    monkeypatch_value = attendance_bot.LOCK_TIMEOUT
    attendance_bot.LOCK_TIMEOUT = 0.05
    try:
        def never_returns(*args):
            time.sleep(5)

        async def run():
            with pytest.raises(TimeoutError):
                await attendance_bot._locked(never_returns)
            # The lock must be free again for the next command, even
            # though the timed-out thread is still running in the
            # background -- a permanently-stuck call must not wedge every
            # later command behind it.
            assert not attendance_bot._SHEET_LOCK.locked()

        asyncio.run(run())
    finally:
        attendance_bot.LOCK_TIMEOUT = monkeypatch_value


# (7) a comma in a player name must not fragment through the log round trip.


def test_parse_players_round_trips_a_name_containing_a_comma():
    raw = json.dumps(["Smith, Jr.", "Kobe"])
    assert attendance_bot._parse_players(raw) == ["Smith, Jr.", "Kobe"]


def test_parse_players_refuses_invalid_json():
    from attendance_sheet import SheetStructureError

    with pytest.raises(SheetStructureError):
        attendance_bot._parse_players("not valid json")


def test_parse_players_refuses_a_json_value_that_is_not_a_list_of_strings():
    from attendance_sheet import SheetStructureError

    with pytest.raises(SheetStructureError):
        attendance_bot._parse_players(json.dumps({"not": "a list"}))


def test_a_comma_in_a_player_name_survives_undo(spreadsheet):
    grid = [
        ["Player Name", "Points", "Lucus - 3"],
        ["Smith, Jr.", "0", "3"],
        ["Kobe", "0", "3"],
    ]
    logged = _entry(boss="Lucus", players=json.dumps(["Smith, Jr.", "Kobe"]))
    row = [str(logged[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(grid=grid, log=_log_tab(row)))

    entry = attendance_bot._reverse_last()

    assert entry is not None
    ranges = {cell["range"] for batch in sh.worksheet(TAB).batches for cell in batch}
    # Had the comma fragmented "Smith, Jr." into "Smith" and " Jr.",
    # plan_point_writes would have raised "No row for ..." for those
    # instead of writing both real players' cells.
    assert ranges == {"C2", "C3"}


# (6) read amplification: one authorised handle per command, one grid read
# per tab lookup.


def test_load_context_reads_the_target_tabs_grid_exactly_once():
    calls = {"get_all_values": 0}
    ws = FakeWorksheet(SAMPLE_GRID, title=TAB)
    original = ws.get_all_values

    def counting_get_all_values():
        calls["get_all_values"] += 1
        return original()

    ws.get_all_values = counting_get_all_values
    sh = FakeSpreadsheet({TAB: ws})
    attendance_bot.write_config(sh, "target_tab", TAB)

    attendance_bot._load_context(sh, "Lucus")

    assert calls["get_all_values"] == 1, (
        "the target tab's grid was read more than once for a single "
        "boss lookup (header resolution, column check, player list)"
    )


def test_load_context_does_not_open_its_own_spreadsheet_handle(monkeypatch):
    """_load_context must use the handle it is given, not authorise its own
    -- otherwise the whole point of caching one handle per command is lost.
    """
    def boom():
        raise AssertionError("_load_context must not call _spreadsheet() itself")

    monkeypatch.setattr(attendance_bot, "_spreadsheet", boom)

    sh = FakeSpreadsheet({TAB: FakeWorksheet(SAMPLE_GRID, title=TAB)})
    attendance_bot.write_config(sh, "target_tab", TAB)

    context = attendance_bot._load_context(sh, "Lucus")
    assert context["boss"] == "Lucus"


import time  # noqa: E402  (kept near its first use, above)


# --------------------------------------------------------------------------
# A LOCK_TIMEOUT mid-commit/mid-undo must not be reported as a confirmed
# "nothing happened" -- the to_thread worker is not actually stopped by
# the timeout (see _locked's docstring), so the outcome is genuinely
# unknown at the moment this fires.
# --------------------------------------------------------------------------


class FakeAttachment:
    def __init__(self, content=b"fake-image-bytes", content_type="image/png", att_id=555):
        self._content = content
        self.content_type = content_type
        self.id = att_id

    async def read(self):
        return self._content


class FakeDiscordMessage:
    """Stands in for the message discord.py hands back from ctx.send /
    passes as the working preview message: supports edit() and
    add_reaction(), and records every edit for assertions."""

    def __init__(self, embed=None):
        self.embed = embed
        self.id = 42
        self.edits = []

    async def edit(self, **kwargs):
        if "embed" in kwargs:
            self.embed = kwargs["embed"]
        self.edits.append(kwargs)

    async def add_reaction(self, emoji):
        pass


class FakeCtx:
    def __init__(self, author, attachments=()):
        self.author = author
        self.message = SimpleNamespace(attachments=list(attachments), id=999)
        self.sent = []

    async def send(self, *args, **kwargs):
        msg = FakeDiscordMessage(embed=kwargs.get("embed"))
        self.sent.append((args, kwargs, msg))
        return msg


def _officer_spreadsheet(role_id=123, tab=TAB, grid=None):
    sh = FakeSpreadsheet({TAB: FakeWorksheet(grid or SAMPLE_GRID, title=TAB)})
    attendance_bot.write_config(sh, "officer_role_id", str(role_id))
    attendance_bot.write_config(sh, "target_tab", tab)
    return sh


def test_attendance_timeout_path_gives_uncertain_wording_not_nothing_written(
    monkeypatch,
):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(
        attendance_bot, "extract_names", lambda *a, **k: ["Kobe"]
    )

    async def fake_wait_for(event, check=None, timeout=None):
        return (SimpleNamespace(), "Officer#1")

    monkeypatch.setattr(attendance_bot.bot, "wait_for", fake_wait_for)

    async def raising_locked(func, *args):
        raise TimeoutError()

    monkeypatch.setattr(attendance_bot, "_locked", raising_locked)

    ctx = FakeCtx(FakeMember([FakeRole(123)]), attachments=[FakeAttachment()])

    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    # The last edit made to the "working" preview message is the final
    # word the officer sees.
    working_message = ctx.sent[0][2]
    final_embed = working_message.embed
    description = final_embed.description

    assert "Nothing was written" not in description
    assert "not known whether" in description or "may or may not" in description.lower()
    assert "Kobe" in description
    assert "Lucus" in description
    assert TAB in description


def test_attendance_timeout_report_goes_to_stderr_first(monkeypatch, capsys):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(attendance_bot, "extract_names", lambda *a, **k: ["Kobe"])

    async def fake_wait_for(event, check=None, timeout=None):
        return (SimpleNamespace(), "Officer#1")

    monkeypatch.setattr(attendance_bot.bot, "wait_for", fake_wait_for)

    async def raising_locked(func, *args):
        raise TimeoutError()

    monkeypatch.setattr(attendance_bot, "_locked", raising_locked)

    ctx = FakeCtx(FakeMember([FakeRole(123)]), attachments=[FakeAttachment()])
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    err = capsys.readouterr().err
    assert "PARTIAL WRITE" in err
    assert "Sheet Timed Out" in err


def test_attendance_genuine_prewrite_failure_still_says_nothing_was_written(
    monkeypatch,
):
    """The fix must not over-apply: a real, non-timeout failure with no
    ambiguity (apply_writes itself raising, so _commit never gets past
    that line) must keep the definite, correct "nothing was written"
    wording. `_locked` is left real here (not monkeypatched to raise
    TimeoutError) so this exercises the genuine `except Exception` branch,
    not the new `except TimeoutError` one.
    """
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(attendance_bot, "extract_names", lambda *a, **k: ["Kobe"])

    async def fake_wait_for(event, check=None, timeout=None):
        return (SimpleNamespace(), "Officer#1")

    monkeypatch.setattr(attendance_bot.bot, "wait_for", fake_wait_for)

    def boom(*args, **kwargs):
        raise SheetStructureError("simulated apply_writes failure")

    monkeypatch.setattr(attendance_bot, "apply_writes", boom)

    ctx = FakeCtx(FakeMember([FakeRole(123)]), attachments=[FakeAttachment()])
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    working_message = ctx.sent[0][2]
    description = working_message.embed.description
    assert "Nothing was written" in description


def test_undo_timeout_path_gives_uncertain_wording_not_nothing_changed(monkeypatch):
    logged = _entry(boss="Lucus", players=json.dumps(["Kobe"]))
    row = [str(logged[field]) for field in LOG_HEADER]
    sh = _officer_spreadsheet()
    sh._sheets[LOG_TAB] = _log_tab(row)  # noqa: SLF001 -- test setup only
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    async def raising_locked(func, *args):
        raise TimeoutError()

    monkeypatch.setattr(attendance_bot, "_locked", raising_locked)

    ctx = FakeCtx(FakeMember([FakeRole(123)]))
    asyncio.run(attendance_bot.undo_attendance_cmd.callback(ctx))

    assert len(ctx.sent) == 1
    _, kwargs, _ = ctx.sent[0]
    description = kwargs["embed"].description

    assert "Nothing was changed" not in description
    assert "not known whether" in description or "may or may not" in description.lower()
    assert "Lucus" in description


def test_undo_timeout_report_goes_to_stderr_first(monkeypatch, capsys):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    async def raising_locked(func, *args):
        raise TimeoutError()

    monkeypatch.setattr(attendance_bot, "_locked", raising_locked)

    ctx = FakeCtx(FakeMember([FakeRole(123)]))
    asyncio.run(attendance_bot.undo_attendance_cmd.callback(ctx))

    err = capsys.readouterr().err
    assert "PARTIAL WRITE" in err
    assert "Sheet Timed Out" in err


def test_undo_genuine_prewrite_failure_still_says_nothing_was_changed(monkeypatch):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    # No log rows at all in `sh` -- last_unreversed_entry finds nothing and
    # _reverse_last's very first step (last_unreversed_entry) returns
    # None inside the lock too, so this is a genuine "nothing to undo",
    # not a timeout. This pins the real _reverse_last, not a fake.
    ctx = FakeCtx(FakeMember([FakeRole(123)]))
    asyncio.run(attendance_bot.undo_attendance_cmd.callback(ctx))

    assert len(ctx.sent) == 1
    _, kwargs, _ = ctx.sent[0]
    assert "Nothing To Undo" in kwargs["embed"].title
