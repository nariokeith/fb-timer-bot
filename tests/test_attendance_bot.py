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


def _reference_lines(name: str) -> list[str]:
    """Code lines mentioning `name`, excluding its own def, and comments."""
    source = inspect.getsource(attendance_bot)
    pattern = re.compile(rf"(?<![\w.]){re.escape(name)}(?![\w])")
    lines = []
    for line in source.splitlines():
        code = line.split("#")[0].strip()
        if not code or code.startswith("def ") or code.startswith("async def "):
            continue
        if pattern.search(code):
            lines.append(code)
    return lines


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
