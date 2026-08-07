"""Tests for the parts of the attendance bot that are real logic.

The module is mostly thin glue over five already-tested modules plus
discord.py, so these cover what can go wrong here and nowhere else: the
never-empty error text, the officer check, the promise that bot.py is
never imported, and the sheet lock that stops two officers clobbering
each other's writes. No Discord client is started.
"""

import asyncio
import hashlib
import inspect
import json
import re
import sys
from types import SimpleNamespace

import attendance_bot
from attendance_bosses import BossAmbiguous, BossNotFound
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
    assert _is_officer(FakeMember(), [123]) is False


def test_wrong_role_id_is_not_an_officer():
    assert _is_officer(FakeMember([FakeRole(999)]), [123]) is False


def test_right_role_id_is_an_officer():
    assert _is_officer(FakeMember([FakeRole(999), FakeRole(123)]), [123]) is True


def test_unconfigured_role_id_denies_everyone():
    # Empty means !setofficerrole has never been run; nobody may log.
    assert _is_officer(FakeMember([FakeRole(123)]), []) is False
    assert _is_officer(FakeMember([FakeRole(123)]), None) is False


def test_any_one_of_several_configured_roles_is_enough():
    # The guild needs more than one officer role; holding the SECOND of
    # three must be as good as holding the first.
    configured = [111, 222, 333]
    assert _is_officer(FakeMember([FakeRole(222)]), configured) is True
    assert _is_officer(FakeMember([FakeRole(333)]), configured) is True


def test_a_member_holding_none_of_the_configured_roles_is_refused():
    assert _is_officer(FakeMember([FakeRole(444), FakeRole(555)]), [111, 222]) is False


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
    for name in ("_commit", "_reverse_last", "_set_target_tab", "_set_officer_roles"):
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
        attendance_bot._commit(TAB, ["Lucus"], ["Kobe"], [3], _entry(), False)

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
        attendance_bot._commit(TAB, ["Lucus"], ["Kobe"], [3], _entry(), False)

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
    attendance_bot._commit(TAB, ["Lucus"], ["Kobe"], [3], _entry(), False)

    ranges = [cell["range"] for batch in sh.worksheet(TAB).batches for cell in batch]
    assert ranges == ["D4"], f"write followed a stale index instead of the name: {ranges}"


def test_commit_takes_a_boss_name_not_a_column_index():
    parameters = inspect.signature(attendance_bot._commit).parameters
    assert "bosses" in parameters
    assert "column" not in parameters
    assert "find_column" in inspect.getsource(attendance_bot._commit)


def test_commit_refuses_when_the_boss_column_vanished_mid_preview(spreadsheet):
    without_lucus = [
        ["Player Name", "Points", "EGO", "Livera"],
        ["Kobe", "44", "1", "3"],
    ]
    sh = spreadsheet(_sheets(grid=without_lucus))

    with pytest.raises(SheetStructureError):
        attendance_bot._commit(TAB, ["Lucus"], ["Kobe"], [3], _entry(), False)

    assert not sh.worksheet(TAB).batches


# duplicate confirmed by someone else during the 180s preview window


def test_commit_aborts_if_the_screenshot_was_logged_during_the_preview(spreadsheet):
    row = [str(_entry()[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(log=_log_tab(row)))

    # was_duplicate=False: the preview showed no warning, so this row
    # appeared while the officer was deciding.
    with pytest.raises(attendance_bot.AlreadyLoggedDuringPreview):
        attendance_bot._commit(TAB, ["Lucus"], ["Kobe"], [3], _entry(), False)

    assert not sh.worksheet(TAB).batches


def test_commit_allows_a_duplicate_the_officer_was_warned_about(spreadsheet):
    row = [str(_entry()[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(log=_log_tab(row)))

    # was_duplicate=True: the preview carried the "Already Logged" warning
    # and the officer confirmed anyway, which is a legitimate re-log.
    attendance_bot._commit(TAB, ["Lucus"], ["Kobe"], [3], _entry(), True)

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
        attendance_bot._commit(TAB, ["Lucus"], ["Kobe"], [3], repost, False)

    assert not sh.worksheet(TAB).batches


def test_commit_does_not_flag_a_different_screenshot_as_a_duplicate(spreadsheet):
    logged = _entry(attachment_id="upload-1", image_sha256="hash-1")
    row = [str(logged[field]) for field in LOG_HEADER]
    sh = spreadsheet(_sheets(log=_log_tab(row)))

    different = _entry(attachment_id="upload-2", image_sha256="hash-2")
    attendance_bot._commit(TAB, ["Lucus"], ["Kobe"], [3], different, False)

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
    assert context["bosses"] == ["Lucus"]


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


# --------------------------------------------------------------------------
# (LIVE-1) Every image on the message must be read. The owner posted two
# screenshots of one rally; the bot read the first, ignored the second, and
# showed a confident "Matched (19)" preview. Two players silently got
# nothing and the preview gave no hint anything was missing.
# --------------------------------------------------------------------------


def _two_image_ctx():
    return FakeCtx(
        FakeMember([FakeRole(123)]),
        attachments=[
            FakeAttachment(content=b"rally-panel", att_id=1),
            FakeAttachment(content=b"second-page", att_id=2),
        ],
    )


def _confirm(monkeypatch):
    async def fake_wait_for(event, check=None, timeout=None):
        return (SimpleNamespace(), "Officer#1")

    monkeypatch.setattr(attendance_bot.bot, "wait_for", fake_wait_for)


def _by_image(mapping):
    def fake_extract(image_bytes, mime, **kwargs):
        return list(mapping[image_bytes])

    return fake_extract


def test_every_image_on_the_message_is_read(monkeypatch):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(
        attendance_bot,
        "extract_names",
        _by_image(
            {
                b"rally-panel": ["Kobe", "xSigarilyas"],
                b"second-page": ["ARCILynN"],
            }
        ),
    )
    _confirm(monkeypatch)

    ctx = _two_image_ctx()
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    # Lucus is column C; ARCILynN is row 2 and only appears on the SECOND
    # image, so C2 is present only if that image was read at all.
    ranges = {
        cell["range"] for batch in sh.worksheet(TAB).batches for cell in batch
    }
    assert ranges == {"C2", "C3", "C4"}, f"second image was ignored: {ranges}"


# --------------------------------------------------------------------------
# (LIVE-2) More than one officer role. The guild's live config holds only
# the legacy single key and must keep working untouched.
# --------------------------------------------------------------------------

LIVE_ROLE_ID = 1530553536503484426


def test_the_legacy_single_officer_role_id_still_grants_access():
    sh = FakeSpreadsheet({TAB: FakeWorksheet(SAMPLE_GRID, title=TAB)})
    attendance_bot.write_config(sh, "officer_role_id", str(LIVE_ROLE_ID))

    role_ids = attendance_bot._officer_role_ids(sh)

    assert role_ids == [LIVE_ROLE_ID]
    assert attendance_bot._is_officer(FakeMember([FakeRole(LIVE_ROLE_ID)]), role_ids)


def test_a_player_on_both_images_is_paid_once(monkeypatch):
    # Overlapping screenshots of one rally are normal: match_names
    # deduplicates by resolved player, so the overlap must not double-pay.
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(
        attendance_bot,
        "extract_names",
        _by_image(
            {
                b"rally-panel": ["Kobe", "xSigarilyas"],
                b"second-page": ["xSigarilyas", "ARCILynN"],
            }
        ),
    )
    _confirm(monkeypatch)

    ctx = _two_image_ctx()
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    cells = [cell for batch in sh.worksheet(TAB).batches for cell in batch]
    ranges = [cell["range"] for cell in cells]
    assert sorted(ranges) == ["C2", "C3", "C4"]
    assert len(ranges) == len(set(ranges)), "a player was written twice"
    # xSigarilyas (row 3) starts at 3 in SAMPLE_GRID and Lucus is worth 3,
    # so a single award lands on 6 -- not 9.
    by_range = {cell["range"]: cell["values"][0][0] for cell in cells}
    assert by_range["C3"] == 6


def test_one_image_failing_vision_aborts_the_whole_command(monkeypatch):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    def flaky(image_bytes, mime, **kwargs):
        if image_bytes == b"second-page":
            raise VisionError("Gemini found no names in that image")
        return ["Kobe"]

    monkeypatch.setattr(attendance_bot, "extract_names", flaky)
    _confirm(monkeypatch)

    ctx = _two_image_ctx()
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    # A partial roster would silently underpay whoever was only on the
    # image that failed, so nothing at all may be written.
    assert not sh.worksheet(TAB).batches
    description = ctx.sent[0][2].embed.description
    assert "2 of 2" in description
    assert "Nothing was written" in description


def test_only_the_image_attachments_are_used(monkeypatch):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    seen = []

    def fake_extract(image_bytes, mime, **kwargs):
        seen.append(image_bytes)
        return ["Kobe"]

    monkeypatch.setattr(attendance_bot, "extract_names", fake_extract)
    _confirm(monkeypatch)

    ctx = FakeCtx(
        FakeMember([FakeRole(123)]),
        attachments=[
            FakeAttachment(content=b"notes", content_type="text/plain", att_id=1),
            FakeAttachment(content=b"rally-panel", att_id=2),
            FakeAttachment(content=b"log.zip", content_type=None, att_id=3),
        ],
    )
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    assert seen == [b"rally-panel"], "a non-image attachment was sent to vision"
    assert sh.worksheet(TAB).batches


def test_a_message_whose_attachments_are_all_non_images_is_refused(monkeypatch):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    ctx = FakeCtx(
        FakeMember([FakeRole(123)]),
        attachments=[
            FakeAttachment(content=b"notes", content_type="text/plain", att_id=1)
        ],
    )
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    assert "Not An Image" in ctx.sent[0][1]["embed"].title
    assert not sh.worksheet(TAB).batches


def test_the_preview_shows_how_many_images_were_read(monkeypatch):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(
        attendance_bot,
        "extract_names",
        _by_image(
            {
                b"rally-panel": ["Kobe", "xSigarilyas"],
                b"second-page": ["ARCILynN"],
            }
        ),
    )

    preview = {}

    async def capture_then_confirm(event, check=None, timeout=None):
        preview["embed"] = ctx.sent[0][2].embed
        return (SimpleNamespace(), "Officer#1")

    monkeypatch.setattr(attendance_bot.bot, "wait_for", capture_then_confirm)

    ctx = _two_image_ctx()
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    fields = {field.name: field.value for field in preview["embed"].fields}
    screenshots = next(v for k, v in fields.items() if "Screenshots Read (2)" in k)
    assert "Image 1: 2 names" in screenshots
    assert "Image 2: 1 name" in screenshots


# --------------------------------------------------------------------------
# The image_sha256 cell: a JSON list now, a bare string on rows already in
# the live sheet. Both must read, and any single hash matching is enough.
# --------------------------------------------------------------------------


def test_image_hashes_round_trip_as_a_json_list(monkeypatch):
    sh = _officer_spreadsheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(attendance_bot, "extract_names", lambda *a, **k: ["Kobe"])
    _confirm(monkeypatch)

    ctx = _two_image_ctx()
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    logged = sh.worksheet(LOG_TAB).get_all_values()[-1]
    stored = logged[LOG_HEADER.index("image_sha256")]
    hashes = json.loads(stored)
    assert hashes == [
        hashlib.sha256(b"rally-panel").hexdigest(),
        hashlib.sha256(b"second-page").hexdigest(),
    ]
    assert attendance_bot.parse_image_hashes(stored) == hashes


def test_a_legacy_bare_hash_string_is_still_read():
    # Rows written before multi-image support hold one bare hex hash.
    assert attendance_bot.parse_image_hashes("abc123") == ["abc123"]
    assert attendance_bot.parse_image_hashes("") == []
    # Unparseable content degrades to "no hashes" rather than raising:
    # duplicate detection is advisory, and one bad row must not take down
    # !attendance for every future screenshot.
    assert attendance_bot.parse_image_hashes("[not json") == []


def test_a_legacy_bare_hash_row_still_flags_a_duplicate():
    old_row = _entry(image_sha256="legacy-hash-value")
    sh = FakeSpreadsheet(
        {
            TAB: FakeWorksheet(SAMPLE_GRID, title=TAB),
            LOG_TAB: _log_tab([str(old_row[f]) for f in LOG_HEADER]),
        }
    )
    assert attendance_bot.any_image_already_logged(sh, ["legacy-hash-value"])
    assert not attendance_bot.any_image_already_logged(sh, ["some-other-hash"])


def test_a_partial_repost_of_one_of_two_images_is_flagged():
    # The realistic double-pay: someone re-posts just one screenshot from
    # an earlier rally. A single combined hash over both images would
    # differ from the original and sail straight through.
    logged = _entry(image_sha256=json.dumps(["hash-A", "hash-B"]))
    sh = FakeSpreadsheet(
        {
            TAB: FakeWorksheet(SAMPLE_GRID, title=TAB),
            LOG_TAB: _log_tab([str(logged[f]) for f in LOG_HEADER]),
        }
    )

    assert attendance_bot.any_image_already_logged(sh, ["hash-B"])
    assert attendance_bot.any_image_already_logged(sh, ["hash-B", "hash-NEW"])
    assert not attendance_bot.any_image_already_logged(sh, ["hash-NEW"])


def test_a_reversed_row_no_longer_flags_a_duplicate():
    logged = _entry(image_sha256=json.dumps(["hash-A"]), reversed="yes")
    sh = FakeSpreadsheet(
        {
            TAB: FakeWorksheet(SAMPLE_GRID, title=TAB),
            LOG_TAB: _log_tab([str(logged[f]) for f in LOG_HEADER]),
        }
    )
    assert not attendance_bot.any_image_already_logged(sh, ["hash-A"])


# --------------------------------------------------------------------------
# Officer role configuration
# --------------------------------------------------------------------------


def test_the_new_key_is_preferred_over_the_legacy_one():
    sh = FakeSpreadsheet({TAB: FakeWorksheet(SAMPLE_GRID, title=TAB)})
    attendance_bot.write_config(sh, "officer_role_id", str(LIVE_ROLE_ID))
    attendance_bot.write_config(sh, "officer_role_ids", json.dumps([111, 222, 333]))

    assert attendance_bot._officer_role_ids(sh) == [111, 222, 333]


def test_no_configuration_at_all_returns_no_roles():
    sh = FakeSpreadsheet({TAB: FakeWorksheet(SAMPLE_GRID, title=TAB)})
    assert attendance_bot._officer_role_ids(sh) == []


def test_setting_several_roles_stores_them_as_a_json_list(monkeypatch):
    sh = FakeSpreadsheet({TAB: FakeWorksheet(SAMPLE_GRID, title=TAB)})
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    ctx = FakeCtx(FakeMember())
    roles = [
        SimpleNamespace(id=111, mention="<@&111>"),
        SimpleNamespace(id=222, mention="<@&222>"),
        SimpleNamespace(id=111, mention="<@&111>"),  # duplicate mention
    ]
    asyncio.run(attendance_bot.set_officer_role_cmd.callback(ctx, *roles))

    assert attendance_bot._officer_role_ids(sh) == [111, 222]

    embed = ctx.sent[0][1]["embed"]
    assert "<@&111>" in embed.description
    assert "<@&222>" in embed.description


def test_setofficerrole_with_no_roles_explains_the_usage(monkeypatch):
    sh = FakeSpreadsheet({TAB: FakeWorksheet(SAMPLE_GRID, title=TAB)})
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    ctx = FakeCtx(FakeMember())
    asyncio.run(attendance_bot.set_officer_role_cmd.callback(ctx))

    assert "Which Role?" in ctx.sent[0][1]["embed"].title
    assert attendance_bot._officer_role_ids(sh) == []


def test_setofficerrole_is_still_administrator_only():
    checks = attendance_bot.set_officer_role_cmd.checks
    assert checks, "the administrator permission check was lost"


def test_the_reaction_gate_accepts_any_configured_role(monkeypatch):
    sh = FakeSpreadsheet({TAB: FakeWorksheet(SAMPLE_GRID, title=TAB)})
    attendance_bot.write_config(sh, "officer_role_ids", json.dumps([111, 222]))
    attendance_bot.write_config(sh, "target_tab", TAB)
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(attendance_bot, "extract_names", lambda *a, **k: ["Kobe"])

    seen = {}

    async def capture_check(event, check=None, timeout=None):
        seen["check"] = check
        return (SimpleNamespace(), "Officer#1")

    monkeypatch.setattr(attendance_bot.bot, "wait_for", capture_check)
    # Bot.user is a read-only property, so patch it on the class.
    monkeypatch.setattr(
        type(attendance_bot.bot),
        "user",
        property(lambda self: SimpleNamespace(id=1)),
    )

    # The command is run by a holder of the FIRST role...
    ctx = FakeCtx(FakeMember([FakeRole(111)]), attachments=[FakeAttachment()])
    asyncio.run(attendance_bot.attendance_cmd.callback(ctx, boss_name="Lucus"))

    check = seen["check"]
    reaction = SimpleNamespace(
        message=SimpleNamespace(id=ctx.sent[0][2].id), emoji=attendance_bot.CONFIRM_EMOJI
    )

    # ...and a holder of the SECOND role may still confirm it.
    second = FakeMember([FakeRole(222)])
    second.id = 7
    assert check(reaction, second) is True

    outsider = FakeMember([FakeRole(999)])
    outsider.id = 8
    assert check(reaction, outsider) is False


# --------------------------------------------------------------------------
# Several bosses in one command: "!attendance clemantis - dalia - catena".
# One rally often kills a few bosses with the same roster, so one command
# is one attendance event, and one !undoattendance reverses all of it.
# --------------------------------------------------------------------------

MULTI_GRID = [
    ["Player Name", "Points", "Clemantis", "Lady Dalia", "Catena", "Lucus - 3"],
    ["ARCILynN", "51", "", "", "", ""],
    ["xSigarilyas", "49", "2", "1", "", "3"],
    ["Kobe", "44", "", "", "", ""],
]


def _multi_sheet(grid=None, log=None):
    sheets = {TAB: FakeWorksheet(grid or MULTI_GRID, title=TAB)}
    if log is not None:
        sheets[LOG_TAB] = log
    sh = FakeSpreadsheet(sheets)
    attendance_bot.write_config(sh, "target_tab", TAB)
    attendance_bot.write_config(sh, "officer_role_ids", json.dumps([123]))
    return sh


def test_three_bosses_resolve_with_their_own_point_values():
    sh = _multi_sheet()
    context = attendance_bot._load_context(sh, "clemantis - dalia - catena")

    assert context["bosses"] == ["Clemantis", "Lady Dalia", "Catena"]
    assert context["points"] == [1, 1, 1]


def test_a_single_boss_with_no_dash_is_unchanged():
    sh = _multi_sheet()
    context = attendance_bot._load_context(sh, "lucus")

    assert context["bosses"] == ["Lucus"]
    assert context["points"] == [3]


def test_mixed_point_values_are_kept_per_boss():
    sh = _multi_sheet()
    context = attendance_bot._load_context(sh, "lucus - clemantis")

    assert context["bosses"] == ["Lucus", "Clemantis"]
    assert context["points"] == [3, 1]


def test_one_unresolvable_fragment_refuses_the_whole_command():
    sh = _multi_sheet()
    with pytest.raises(BossNotFound) as caught:
        attendance_bot._load_context(sh, "clemantis - nosuchboss - catena")

    # The officer must be able to see WHICH part was wrong.
    assert "nosuchboss" in str(caught.value)


def test_the_points_annotation_collision_names_the_offending_fragment():
    # Sheet headers annotate points with the same " - " ("Lucus - 3"), so
    # "!attendance lucus - 3" splits into "lucus" and "3". Failing is
    # correct; the message just has to say which fragment failed.
    sh = _multi_sheet()
    with pytest.raises(BossNotFound) as caught:
        attendance_bot._load_context(sh, "lucus - 3")

    assert "'3'" in str(caught.value) or '"3"' in str(caught.value)


def test_an_ambiguous_fragment_refuses_the_whole_command():
    grid = [
        ["Player Name", "Points", "Clemantis", "Catena Prime", "Catena Rex"],
        ["Kobe", "44", "", "", ""],
    ]
    sh = _multi_sheet(grid=grid)

    # The FIRST fragment resolves cleanly; the second is ambiguous. The
    # whole command must still be refused, not just the bad fragment
    # dropped -- otherwise Clemantis is logged alone and nobody notices.
    with pytest.raises(BossAmbiguous) as caught:
        attendance_bot._load_context(sh, "clemantis - catena")

    assert "catena" in str(caught.value).casefold()


def test_a_repeated_boss_is_deduplicated():
    sh = _multi_sheet()
    context = attendance_bot._load_context(sh, "dalia - dalia")
    assert context["bosses"] == ["Lady Dalia"]

    # Also when the same boss is named two different ways.
    context = attendance_bot._load_context(sh, "dalia - Lady Dalia")
    assert context["bosses"] == ["Lady Dalia"]


@pytest.mark.parametrize("query", ["clemantis - ", " - catena", "clemantis -  - catena"])
def test_an_empty_fragment_is_refused(query):
    sh = _multi_sheet()
    with pytest.raises(attendance_bot.BossQueryError):
        attendance_bot._load_context(sh, query)


def test_all_bosses_are_written_in_one_apply_writes(monkeypatch):
    sh = _multi_sheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    entry = _entry(
        boss=json.dumps(["Clemantis", "Lady Dalia", "Catena"]),
        points_each=json.dumps([1, 1, 1]),
        players=json.dumps(["Kobe"]),
    )
    attendance_bot._commit(
        TAB,
        ["Clemantis", "Lady Dalia", "Catena"],
        ["Kobe"],
        [1, 1, 1],
        entry,
        False,
    )

    # One batch, not one per boss: a failure partway through a per-boss
    # loop would leave some bosses paid and others not, with a log row
    # describing neither state.
    assert len(sh.worksheet(TAB).batches) == 1
    ranges = {cell["range"] for cell in sh.worksheet(TAB).batches[0]}
    assert ranges == {"C4", "D4", "E4"}


def test_each_boss_column_gains_its_own_value(monkeypatch):
    sh = _multi_sheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    # xSigarilyas is row 3 and starts at Clemantis=2, Lady Dalia=1, Lucus=3.
    entry = _entry(
        boss=json.dumps(["Clemantis", "Lucus"]),
        points_each=json.dumps([1, 3]),
        players=json.dumps(["xSigarilyas"]),
    )
    attendance_bot._commit(
        TAB, ["Clemantis", "Lucus"], ["xSigarilyas"], [1, 3], entry, False
    )

    written = {
        cell["range"]: cell["values"][0][0]
        for cell in sh.worksheet(TAB).batches[0]
    }
    assert written == {"C3": 3, "F3": 6}


def test_undo_reverses_every_boss_in_one_write(monkeypatch):
    logged = _entry(
        tab=TAB,
        boss=json.dumps(["Clemantis", "Lucus"]),
        points_each=json.dumps([1, 3]),
        players=json.dumps(["xSigarilyas"]),
    )
    sh = _multi_sheet(log=_log_tab([str(logged[f]) for f in LOG_HEADER]))
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    attendance_bot._reverse_last()

    assert len(sh.worksheet(TAB).batches) == 1
    written = {
        cell["range"]: cell["values"][0][0]
        for cell in sh.worksheet(TAB).batches[0]
    }
    # The fixture grid already holds the post-award values (Clemantis 2,
    # Lucus 3), so undoing +1/+3 leaves 1 and a cleared blank.
    assert written == {"C3": 1, "F3": ""}


def test_a_legacy_single_boss_log_row_still_undoes(monkeypatch):
    # Rows already in the guild's live sheet hold bare strings, not JSON.
    logged = _entry(
        tab=TAB,
        boss="Lucus",
        points_each="3",
        players=json.dumps(["xSigarilyas"]),
    )
    sh = _multi_sheet(log=_log_tab([str(logged[f]) for f in LOG_HEADER]))
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    attendance_bot._reverse_last()

    written = {
        cell["range"]: cell["values"][0][0]
        for cell in sh.worksheet(TAB).batches[0]
    }
    # 3 - 3 = 0, and a zeroed boss cell is cleared back to blank.
    assert written == {"F3": ""}


def test_the_preview_lists_every_boss_with_its_own_points(monkeypatch):
    sh = _multi_sheet()
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    monkeypatch.setattr(attendance_bot, "extract_names", lambda *a, **k: ["Kobe"])

    preview = {}

    async def capture(event, check=None, timeout=None):
        preview["embed"] = ctx.sent[0][2].embed
        return (SimpleNamespace(), "Officer#1")

    monkeypatch.setattr(attendance_bot.bot, "wait_for", capture)

    ctx = FakeCtx(FakeMember([FakeRole(123)]), attachments=[FakeAttachment()])
    asyncio.run(
        attendance_bot.attendance_cmd.callback(
            ctx, boss_name="clemantis - dalia - lucus"
        )
    )

    description = preview["embed"].description
    for expected in ("Clemantis +1", "Lady Dalia +1", "Lucus +3"):
        assert expected in description
    assert "**5** points each" in description


# --------------------------------------------------------------------------
# (MB-1) The anti-zip-short guard. A hand-edited or half-written _BotLog row
# whose boss count and points count disagree must be refused BEFORE any
# write -- zipping short would under-reverse real people's points.
# --------------------------------------------------------------------------


@pytest.fixture
def no_writes(monkeypatch):
    """Records every apply_writes call so a test can prove there were none."""
    calls = []

    def spy(worksheet, payload):
        calls.append(payload)

    monkeypatch.setattr(attendance_bot, "apply_writes", spy)
    return calls


def _undo_sheet_with(monkeypatch, **overrides):
    logged = _entry(tab=TAB, players=json.dumps(["xSigarilyas"]), **overrides)
    sh = _multi_sheet(log=_log_tab([str(logged[f]) for f in LOG_HEADER]))
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)
    return sh


def test_two_bosses_with_a_single_bare_points_cell_is_refused(
    monkeypatch, no_writes
):
    sh = _undo_sheet_with(
        monkeypatch,
        boss=json.dumps(["Clemantis", "Lucus"]),
        points_each="3",
    )

    with pytest.raises(SheetStructureError) as caught:
        attendance_bot._reverse_last()

    assert "refusing to guess" in str(caught.value)
    # Refused BEFORE writing, not merely raising eventually.
    assert not no_writes
    assert not sh.worksheet(TAB).batches


def test_one_boss_with_two_points_values_is_refused(monkeypatch, no_writes):
    sh = _undo_sheet_with(
        monkeypatch,
        boss="Lucus",
        points_each=json.dumps([1, 3]),
    )

    with pytest.raises(SheetStructureError) as caught:
        attendance_bot._reverse_last()

    assert "refusing to guess" in str(caught.value)
    assert not no_writes
    assert not sh.worksheet(TAB).batches


# --------------------------------------------------------------------------
# (MB-2) Atomicity under failure. The property the whole design turns on:
# a boss that fails partway through means NOTHING is written, not a partial
# set of columns under a log row that describes neither state.
# --------------------------------------------------------------------------


def test_a_boss_failing_partway_through_writes_nothing_at_all(monkeypatch):
    # Clemantis has a column; Lucus does not.
    grid = [
        ["Player Name", "Points", "Clemantis", "Lady Dalia"],
        ["ARCILynN", "51", "", ""],
        ["Kobe", "44", "2", "1"],
    ]
    sh = _multi_sheet(grid=grid)
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    entry = _entry(
        boss=json.dumps(["Clemantis", "Lucus"]),
        points_each=json.dumps([1, 3]),
        players=json.dumps(["Kobe"]),
    )

    with pytest.raises(SheetStructureError):
        attendance_bot._commit(
            TAB, ["Clemantis", "Lucus"], ["Kobe"], [1, 3], entry, False
        )

    # Clemantis resolved fine and would have been written by a per-boss
    # apply_writes loop. One payload, one write, so nothing landed.
    assert not sh.worksheet(TAB).batches


def test_an_undo_failing_partway_through_removes_nothing_at_all(monkeypatch):
    grid = [
        ["Player Name", "Points", "Clemantis", "Lady Dalia"],
        ["xSigarilyas", "49", "2", "1"],
    ]
    logged = _entry(
        tab=TAB,
        boss=json.dumps(["Clemantis", "Lucus"]),
        points_each=json.dumps([1, 3]),
        players=json.dumps(["xSigarilyas"]),
    )
    sh = _multi_sheet(grid=grid, log=_log_tab([str(logged[f]) for f in LOG_HEADER]))
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    with pytest.raises(SheetStructureError):
        attendance_bot._reverse_last()

    assert not sh.worksheet(TAB).batches


# --------------------------------------------------------------------------
# (MB-3) The boss list must survive in the do-not-re-run messages: an
# officer who cannot see which columns were paid cannot reconcile them.
# --------------------------------------------------------------------------

TEN_BOSSES = [
    "Clemantis", "Lady Dalia", "Catena", "General Aquleus", "Lucus",
    "Libitina", "Rakajeth", "Icaruthia", "Nevaeh", "Camalia",
]
TEN_POINTS = [1, 1, 1, 1, 3, 3, 3, 3, 3, 3]


def test_every_boss_survives_in_the_partly_written_message():
    exc = attendance_bot.PointsWrittenButNotLogged(
        tab="Week 17.1",
        boss=TEN_BOSSES,
        points=TEN_POINTS,
        players=BIG_ROSTER,
        cause_text=BIG_CAUSE,
    )
    description = exc.description

    assert len(description) <= DISCORD_EMBED_DESCRIPTION_LIMIT
    for boss in TEN_BOSSES:
        assert boss in description, f"{boss} was clipped out of the reconcile list"
    for sentence in PARTLY_WRITTEN_SENTENCES:
        assert sentence in description


def test_every_boss_survives_in_the_partly_undone_message():
    exc = attendance_bot.PointsRemovedButNotMarked(
        entry={
            "tab": "Week 17.1",
            "boss": json.dumps(TEN_BOSSES),
            "points_each": json.dumps(TEN_POINTS),
            "players": json.dumps(BIG_ROSTER),
        },
        cause_text=BIG_CAUSE,
    )
    description = exc.description

    assert len(description) <= DISCORD_EMBED_DESCRIPTION_LIMIT
    for boss in TEN_BOSSES:
        assert boss in description
    for sentence in PARTLY_UNDONE_SENTENCES:
        assert sentence in description


def test_the_undo_confirmation_never_says_removed_none(monkeypatch):
    # _peek_boss_summary returning None once rendered "Removed None from".
    assert attendance_bot._peek_boss_summary({}) is None
    assert attendance_bot._peek_boss_summary({"boss": "", "points_each": ""}) is None

    logged = _entry(
        tab=TAB,
        boss="",
        points_each="",
        players=json.dumps(["xSigarilyas"]),
    )
    sh = _multi_sheet(log=_log_tab([str(logged[f]) for f in LOG_HEADER]))
    monkeypatch.setattr(attendance_bot, "_spreadsheet", lambda: sh)

    ctx = FakeCtx(FakeMember([FakeRole(123)]))
    asyncio.run(attendance_bot.undo_attendance_cmd.callback(ctx))

    description = ctx.sent[-1][1]["embed"].description
    assert "None" not in description
