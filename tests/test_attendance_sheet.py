import gspread
import gspread.utils
import pytest

import attendance_sheet
from attendance_sheet import (
    SheetStructureError,
    apply_writes,
    find_column,
    plan_point_writes,
    read_headers,
    read_players,
)
from conftest import SAMPLE_GRID, FakeSpreadsheet, FakeWorksheet


@pytest.fixture
def ws():
    return FakeWorksheet(SAMPLE_GRID)


def test_reads_the_header_row(ws):
    assert read_headers(ws)[:3] == ["Player Name", "Points", "Lucus - 3"]


def test_reads_players_from_column_a_skipping_the_header(ws):
    assert read_players(ws) == ["ARCILynN", "xSigarilyas", "Kobe", "wileKAMOTE卐"]


def test_finds_a_column_by_its_boss_name(ws):
    assert find_column(ws, "EGO") == 4
    assert find_column(ws, "Lady Dalia") == 6


def test_finds_a_column_whose_header_carries_a_point_annotation(ws):
    assert find_column(ws, "Lucus") == 3


def test_missing_column_names_what_it_wanted(ws):
    with pytest.raises(SheetStructureError, match="Venatus"):
        find_column(ws, "Venatus")


def test_adds_points_to_the_existing_value(ws):
    payload = plan_point_writes(ws, ["ARCILynN", "Kobe"], 4, 1)
    assert payload == [
        {"range": "D2", "values": [[2]]},
        {"range": "D4", "values": [[2]]},
    ]


def test_treats_a_blank_cell_as_zero(ws):
    assert plan_point_writes(ws, ["ARCILynN"], 3, 3) == [
        {"range": "C2", "values": [[3]]}
    ]


def test_negative_points_subtract_for_undo(ws):
    # Kobe's Lady Dalia cell is "2"; -2 nets to exactly 0, which is written
    # back as blank rather than the integer 0 (see
    # test_a_result_of_exactly_zero_is_written_as_blank_not_the_integer_zero
    # for the dedicated coverage of that rule).
    assert plan_point_writes(ws, ["Kobe"], 6, -2) == [
        {"range": "F4", "values": [[""]]}
    ]


def test_refuses_to_write_the_points_column(ws):
    with pytest.raises(SheetStructureError, match="formula"):
        plan_point_writes(ws, ["ARCILynN"], 2, 1)


def test_a_missing_player_aborts_the_whole_write(ws):
    with pytest.raises(SheetStructureError, match="Ghost"):
        plan_point_writes(ws, ["Kobe", "Ghost"], 4, 1)


def test_column_lookup_survives_a_reordered_sheet():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO", "Lucus - 3", "Livera"],
        ["Kobe", "44", "1", "", "3"],
    ])
    assert find_column(ws, "Lucus") == 4
    assert find_column(ws, "EGO") == 3


def test_apply_writes_sends_one_batch(ws):
    payload = plan_point_writes(ws, ["ARCILynN", "Kobe", "xSigarilyas"], 4, 1)
    apply_writes(ws, payload)
    assert len(ws.batches) == 1
    assert len(ws.batches[0]) == 3


def test_apply_writes_does_nothing_when_there_is_nothing_to_write(ws):
    apply_writes(ws, [])
    assert ws.batches == []


def test_installed_gspread_actually_has_the_api_the_module_calls():
    """Guard against the Task 4 failure mode: green tests that only pass
    because FakeWorksheet has whatever methods we gave it, while the real
    gspread library has drifted (or never had) the attribute being called.

    This test imports the REAL gspread -- never FakeWorksheet -- and checks
    every symbol attendance_sheet.py actually calls.
    """
    import gspread
    import gspread.utils
    import gspread.exceptions

    version = getattr(gspread, "__version__", "unknown")

    top_level = ["authorize"]
    for name in top_level:
        assert hasattr(gspread, name), (
            f"gspread=={version} has no top-level `{name}` attribute, but "
            f"attendance_sheet.py calls gspread.{name}(...)"
        )

    assert hasattr(gspread.utils, "rowcol_to_a1"), (
        f"gspread=={version} has no gspread.utils.rowcol_to_a1, but "
        "plan_point_writes() depends on it to build cell ranges"
    )

    assert hasattr(gspread.exceptions, "WorksheetNotFound"), (
        f"gspread=={version} has no gspread.exceptions.WorksheetNotFound"
    )

    worksheet_methods = [
        "get_all_values",
        "batch_update",
        "append_row",
        "update_cell",
    ]
    for name in worksheet_methods:
        assert hasattr(gspread.Worksheet, name), (
            f"gspread=={version} Worksheet has no `{name}` method, but "
            f"attendance_sheet.py calls worksheet.{name}(...)"
        )

    spreadsheet_methods = ["worksheet", "add_worksheet"]
    for name in spreadsheet_methods:
        assert hasattr(gspread.Spreadsheet, name), (
            f"gspread=={version} Spreadsheet has no `{name}` method, but "
            f"the attendance flow calls spreadsheet.{name}(...)"
        )

    # open_spreadsheet() is the only place that calls gspread.authorize(...)
    # .open_by_key(...) and Credentials.from_service_account_info(...); it
    # has no fake and no unit test of its own, so this guard test is the
    # only thing standing between a drifted API and a live failure.
    assert hasattr(gspread.Client, "open_by_key"), (
        f"gspread=={version} Client has no `open_by_key` method, but "
        "open_spreadsheet() calls gspread.authorize(creds).open_by_key(...)"
    )

    assert hasattr(gspread.Client, "set_timeout"), (
        f"gspread=={version} Client has no `set_timeout` method, but "
        "open_spreadsheet() calls client.set_timeout(...) to bound every "
        "request so a black-holed connection cannot wedge the sheet lock "
        "forever"
    )

    from google.oauth2.service_account import Credentials

    google_auth_version = None
    try:
        import google.auth

        google_auth_version = getattr(google.auth, "__version__", "unknown")
    except Exception:
        google_auth_version = "unknown"

    assert hasattr(Credentials, "from_service_account_info"), (
        f"google-auth=={google_auth_version}: "
        "google.oauth2.service_account.Credentials has no "
        "`from_service_account_info` classmethod, but open_spreadsheet() "
        "calls Credentials.from_service_account_info(...)"
    )

    # The exact addressing convention plan_point_writes() relies on: a
    # silent change here (e.g. swapping row/col order) would write points
    # to the wrong cell without any other test noticing, because every
    # other test goes through FakeWorksheet's own bookkeeping.
    assert gspread.utils.rowcol_to_a1(2, 4) == "D2", (
        f"gspread=={version} gspread.utils.rowcol_to_a1(2, 4) != 'D2' -- "
        "the row/column addressing convention changed, which would silently "
        "misdirect every cell write in plan_point_writes()"
    )


def test_refuses_a_non_numeric_boss_cell_naming_cell_and_content():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "x"],
    ])
    with pytest.raises(SheetStructureError, match="C2") as excinfo:
        plan_point_writes(ws, ["ARCILynN"], 3, 1)
    message = str(excinfo.value)
    assert "C2" in message
    assert "x" in message


def test_refuses_a_comma_formatted_number_instead_of_losing_it():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "1,000"],
    ])
    with pytest.raises(SheetStructureError, match="1,000"):
        plan_point_writes(ws, ["ARCILynN"], 3, 1)


def test_refuses_an_infinite_cell_as_a_structure_error_not_a_crash():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "inf"],
    ])
    with pytest.raises(SheetStructureError, match="inf"):
        plan_point_writes(ws, ["ARCILynN"], 3, 1)


def test_refuses_a_decimal_cell_instead_of_truncating_it():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "3.7"],
    ])
    with pytest.raises(SheetStructureError, match="C2") as excinfo:
        plan_point_writes(ws, ["ARCILynN"], 3, 3)
    message = str(excinfo.value)
    assert "C2" in message
    assert "3.7" in message
    assert "whole number" in message


def test_accepts_a_whole_number_rendered_with_a_trailing_point_zero():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "5.0"],
    ])
    assert plan_point_writes(ws, ["ARCILynN"], 3, 1) == [
        {"range": "C2", "values": [[6]]}
    ]


def test_a_negative_whole_number_cell_still_parses_normally():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "-2"],
    ])
    assert plan_point_writes(ws, ["ARCILynN"], 3, 1) == [
        {"range": "C2", "values": [[-1]]}
    ]


def test_a_plain_whole_number_cell_still_parses_normally():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "5"],
    ])
    assert plan_point_writes(ws, ["ARCILynN"], 3, 1) == [
        {"range": "C2", "values": [[6]]}
    ]


def test_blank_cell_still_yields_zero_after_the_whole_number_check(ws):
    assert plan_point_writes(ws, ["ARCILynN"], 3, 3) == [
        {"range": "C2", "values": [[3]]}
    ]


def test_a_result_of_exactly_zero_is_written_as_blank_not_the_integer_zero():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "1"],
    ])
    assert plan_point_writes(ws, ["ARCILynN"], 3, -1) == [
        {"range": "C2", "values": [[""]]}
    ]


def test_a_nonzero_result_after_subtracting_still_writes_an_integer():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "3"],
    ])
    assert plan_point_writes(ws, ["ARCILynN"], 3, -1) == [
        {"range": "C2", "values": [[2]]}
    ]


def test_a_nonzero_result_after_adding_still_writes_an_integer():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "1"],
    ])
    assert plan_point_writes(ws, ["ARCILynN"], 3, 1) == [
        {"range": "C2", "values": [[2]]}
    ]


def test_a_blank_cell_plus_one_still_writes_the_integer_one():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", ""],
    ])
    assert plan_point_writes(ws, ["ARCILynN"], 3, 1) == [
        {"range": "C2", "values": [[1]]}
    ]


def _apply_to_fake_grid(ws, payload):
    """Mirror a batch payload into FakeWorksheet's own grid.

    FakeWorksheet.batch_update only records what was sent (matching what a
    real gspread mock would assert on); it does not mutate `_rows`. This
    test needs the grid to actually reflect each write to prove the
    round-trip, so it replays the payload through update_cell -- which
    FakeWorksheet does apply -- the same way real Sheets would apply a
    batch_update to the live cells.
    """
    apply_writes(ws, payload)
    for cell in payload:
        row, col = gspread.utils.a1_to_rowcol(cell["range"])
        ws.update_cell(row, col, cell["values"][0][0])


def test_blank_then_commit_then_undo_then_commit_again_reads_one_not_two():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", ""],
    ])

    commit_payload = plan_point_writes(ws, ["ARCILynN"], 3, 1)
    assert commit_payload == [{"range": "C2", "values": [[1]]}]
    _apply_to_fake_grid(ws, commit_payload)

    undo_payload = plan_point_writes(ws, ["ARCILynN"], 3, -1)
    assert undo_payload == [{"range": "C2", "values": [[""]]}]
    _apply_to_fake_grid(ws, undo_payload)

    recommit_payload = plan_point_writes(ws, ["ARCILynN"], 3, 1)
    assert recommit_payload == [{"range": "C2", "values": [[1]]}]


def test_a_negative_result_still_writes_the_negative_integer_not_blank():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["ARCILynN", "51", "1"],
    ])
    assert plan_point_writes(ws, ["ARCILynN"], 3, -2) == [
        {"range": "C2", "values": [[-1]]}
    ]


def test_duplicate_player_rows_abort_naming_both_rows():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO"],
        ["Kobe", "44", "1"],
        ["ARCILynN", "51", ""],
        ["Kobe", "10", ""],
    ])
    with pytest.raises(SheetStructureError, match=r"Kobe.*\(2 and 4\)"):
        plan_point_writes(ws, ["Kobe"], 3, 1)


def test_duplicate_boss_columns_abort_naming_both_indices():
    ws = FakeWorksheet([
        ["Player Name", "Points", "Livera", "EGO", "Livera - 3"],
        ["Kobe", "44", "1", "1", ""],
    ])
    with pytest.raises(SheetStructureError, match=r"columns 3, 5"):
        find_column(ws, "Livera")


def test_empty_boss_name_refuses_rather_than_returning_a_spacer_column(ws):
    with pytest.raises(SheetStructureError, match="blank"):
        find_column(ws, "")
    with pytest.raises(SheetStructureError, match="blank"):
        find_column(ws, "   ")


def test_malformed_service_account_json_does_not_leak_the_input():
    from attendance_sheet import open_spreadsheet

    secret_blob = '{"type": "service_account", "private_key": "TOP-SECRET-KEY"'  # noqa: E501 -- deliberately truncated/invalid JSON
    with pytest.raises(SheetStructureError) as excinfo:
        open_spreadsheet("some-sheet-id", secret_blob)

    assert excinfo.value.__cause__ is None
    assert "TOP-SECRET-KEY" not in str(excinfo.value)


import gspread

from attendance_sheet import (
    CONFIG_HEADER,
    CONFIG_TAB,
    LOG_HEADER,
    LOG_TAB,
    append_log_entry,
    get_or_create_tab,
    image_already_logged,
    last_unreversed_entry,
    mark_entry_reversed,
    read_config,
    write_config,
)
from conftest import FakeSpreadsheet


def _entry(**overrides):
    base = {
        "timestamp": "2026-08-06T21:00:00",
        "tab": "Week 17",
        "boss": "Lucus",
        "points_each": 3,
        "message_id": "111",
        "attachment_id": "aaa",
        "image_sha256": "hash-aaa",
        "confirmed_by": "officer#1",
        "players": "Kobe, Talong",
        "reversed": "",
    }
    base.update(overrides)
    return base


def test_creates_a_tab_with_its_header_when_missing():
    sh = FakeSpreadsheet()
    ws = get_or_create_tab(sh, LOG_TAB, LOG_HEADER)
    assert sh.created == [LOG_TAB]
    assert ws.appended == [LOG_HEADER]


def test_reuses_an_existing_tab():
    sh = FakeSpreadsheet({LOG_TAB: FakeWorksheet([LOG_HEADER], title=LOG_TAB)})
    get_or_create_tab(sh, LOG_TAB, LOG_HEADER)
    assert sh.created == []


def test_config_round_trips():
    sh = FakeSpreadsheet()
    write_config(sh, "target_tab", "Week 17")
    write_config(sh, "officer_role_id", "12345")
    assert read_config(sh) == {"target_tab": "Week 17", "officer_role_id": "12345"}


def test_writing_an_existing_config_key_replaces_it():
    sh = FakeSpreadsheet()
    write_config(sh, "target_tab", "Week 17")
    write_config(sh, "target_tab", "Week 17.1")
    assert read_config(sh) == {"target_tab": "Week 17.1"}


def test_config_is_empty_when_the_tab_does_not_exist():
    assert read_config(FakeSpreadsheet()) == {}


def test_log_entry_is_appended_in_header_order():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry())
    ws = sh.worksheet(LOG_TAB)
    assert ws.appended[0] == LOG_HEADER
    assert ws.appended[1][LOG_HEADER.index("boss")] == "Lucus"
    assert ws.appended[1][LOG_HEADER.index("players")] == "Kobe, Talong"


def test_detects_a_screenshot_that_was_already_logged():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(image_sha256="hash-aaa"))
    assert image_already_logged(sh, "hash-aaa") is True
    assert image_already_logged(sh, "hash-bbb") is False


def test_a_new_attachment_id_for_the_same_image_still_counts_as_a_duplicate():
    """The actual defect this module exists to fix: Discord mints a new
    attachment_id on every upload, so a genuine repost of the same picture
    must still be caught by its (unchanged) image hash even though its
    attachment_id is different every time.
    """
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(attachment_id="first-upload", image_sha256="hash-aaa"))
    assert image_already_logged(sh, "hash-aaa") is True


def test_reversed_entries_do_not_count_as_already_logged():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(attachment_id="aaa", image_sha256="hash-aaa"))
    row_number, entry = last_unreversed_entry(sh)
    mark_entry_reversed(sh, row_number, entry["attachment_id"])
    assert image_already_logged(sh, "hash-aaa") is False


def test_last_unreversed_entry_returns_the_most_recent():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(boss="Lucus", attachment_id="aaa"))
    append_log_entry(sh, _entry(boss="Motti", attachment_id="bbb"))
    row_number, entry = last_unreversed_entry(sh)
    assert entry["boss"] == "Motti"
    assert row_number == 3  # header + two entries


def test_last_unreversed_entry_skips_reversed_ones():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(boss="Lucus", attachment_id="aaa"))
    append_log_entry(sh, _entry(boss="Motti", attachment_id="bbb"))
    mark_entry_reversed(sh, 3, "bbb")
    _, entry = last_unreversed_entry(sh)
    assert entry["boss"] == "Lucus"


def test_last_unreversed_entry_is_none_when_there_is_nothing_to_undo():
    assert last_unreversed_entry(FakeSpreadsheet()) is None


def test_log_entry_round_trips_every_field_through_log_header_order():
    """Pins the write/read contract that undo depends on: append_log_entry
    writes positionally from LOG_HEADER and _log_rows (via
    last_unreversed_entry) zips the row back against the same LOG_HEADER. If
    the two ever disagreed -- say LOG_HEADER got reordered on one side only
    -- fields would be silently misassigned and undo would subtract the
    wrong points from the wrong people without any test noticing.
    """
    sh = FakeSpreadsheet()
    written = _entry(
        timestamp="2026-08-06T21:00:00",
        tab="Week 17",
        boss="Lucus",
        points_each=3,
        message_id="111",
        attachment_id="aaa",
        confirmed_by="officer#1",
        players="Kobe, Talong",
        reversed="",
    )
    append_log_entry(sh, written)
    _, read_back = last_unreversed_entry(sh)

    for field, value in written.items():
        assert read_back[field] == str(value), field


def test_a_short_log_row_from_an_older_version_pads_without_shifting_fields():
    """A log row written before a LOG_HEADER field was added would come back
    from the sheet with fewer cells than LOG_HEADER. Padding must add blanks
    at the end, not at the start or middle -- otherwise every field after
    the pad point (attachment_id, confirmed_by, players, reversed) would be
    read from the wrong column.
    """
    short_row = ["2026-08-06T21:00:00", "Week 17", "Lucus", "3", "111"]
    sh = FakeSpreadsheet({LOG_TAB: FakeWorksheet([LOG_HEADER, short_row], title=LOG_TAB)})

    _, entry = last_unreversed_entry(sh)

    assert entry["timestamp"] == "2026-08-06T21:00:00"
    assert entry["tab"] == "Week 17"
    assert entry["boss"] == "Lucus"
    assert entry["points_each"] == "3"
    assert entry["message_id"] == "111"
    assert entry["attachment_id"] == ""
    assert entry["image_sha256"] == ""
    assert entry["confirmed_by"] == ""
    assert entry["players"] == ""
    assert entry["reversed"] == ""


# --- Fix round 1 -------------------------------------------------------


def test_a_log_tab_whose_header_no_longer_matches_LOG_HEADER_is_refused():
    """(t) The real hazard: a live _BotLog tab created under an OLD field
    order. If _log_rows zipped blindly against the current LOG_HEADER, a
    reordered field (attachment_id and confirmed_by swapped here) would be
    silently misread -- undo would then subtract points using the wrong
    attachment_id/confirmed_by pairing. This must raise instead of guess.
    """
    old_header = [
        "timestamp",
        "tab",
        "boss",
        "points_each",
        "message_id",
        "confirmed_by",  # swapped with attachment_id relative to LOG_HEADER
        "attachment_id",
        "players",
        "reversed",
    ]
    old_row = [
        "2026-08-06T21:00:00",
        "Week 17",
        "Lucus",
        "3",
        "111",
        "officer#1",
        "aaa",
        "Kobe, Talong",
        "",
    ]
    sh = FakeSpreadsheet({LOG_TAB: FakeWorksheet([old_header, old_row], title=LOG_TAB)})

    with pytest.raises(SheetStructureError, match="row 1"):
        last_unreversed_entry(sh)

    with pytest.raises(SheetStructureError, match="row 1"):
        image_already_logged(sh, "aaa")

    with pytest.raises(SheetStructureError, match="row 1"):
        append_log_entry(sh, _entry())


def test_duplicate_config_key_is_refused_by_both_read_and_write():
    """(u) write_config used to update only the FIRST matching row while
    read_config's dict comprehension let the LAST duplicate win -- so a
    successful write could read back as an unrelated older value. Refusing
    on the duplicate (like _rows_by_player does for players) makes both
    sides agree: never silently disagree.
    """
    sh = FakeSpreadsheet(
        {
            CONFIG_TAB: FakeWorksheet(
                [
                    CONFIG_HEADER,
                    ["target_tab", "Week 17"],
                    ["target_tab", "Week 18"],
                ],
                title=CONFIG_TAB,
            )
        }
    )
    with pytest.raises(SheetStructureError, match=r"target_tab.*\(2 and 3\)"):
        read_config(sh)
    with pytest.raises(SheetStructureError, match=r"target_tab.*\(2 and 3\)"):
        write_config(sh, "target_tab", "Week 19")


def test_write_config_strips_the_incoming_key_so_whitespace_cannot_hide_a_match():
    sh = FakeSpreadsheet()
    write_config(sh, "target_tab", "Week 17")
    write_config(sh, "  target_tab  ", "Week 18")
    assert read_config(sh) == {"target_tab": "Week 18"}
    ws = sh.worksheet(CONFIG_TAB)
    assert len(ws.get_all_values()) == 2  # header + one row, not two


def test_mark_entry_reversed_refuses_when_the_row_changed_underneath_it():
    """(v) Simulates the undo race: officer A reads row 3 (last unreversed,
    attachment_id 'bbb'), but before A's write lands, the row is no longer
    what A read (attachment_id has changed -- e.g. another entry's write
    landed on the same physical row, or the row was edited). A's write
    must refuse rather than mark the wrong entry reversed.
    """
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(boss="Lucus", attachment_id="aaa"))
    append_log_entry(sh, _entry(boss="Motti", attachment_id="bbb"))
    row_number, entry = last_unreversed_entry(sh)
    assert row_number == 3
    assert entry["attachment_id"] == "bbb"

    ws = sh.worksheet(LOG_TAB)
    ws.update_cell(row_number, LOG_HEADER.index("attachment_id") + 1, "ccc")

    with pytest.raises(SheetStructureError, match="ccc"):
        mark_entry_reversed(sh, row_number, "bbb")


def test_mark_entry_reversed_refuses_to_double_reverse():
    """(v) The second of two concurrent !undoattendance calls must fail
    loudly instead of subtracting the same entry's points twice.
    """
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(attachment_id="aaa"))
    row_number, entry = last_unreversed_entry(sh)
    mark_entry_reversed(sh, row_number, entry["attachment_id"])

    with pytest.raises(SheetStructureError, match="already marked reversed"):
        mark_entry_reversed(sh, row_number, entry["attachment_id"])


class _FakeApiErrorResponse:
    """Minimal stand-in for the requests.Response gspread.exceptions.APIError wraps."""

    text = '{"error": {"code": 400, "message": "already exists"}}'

    def json(self):
        return {"error": {"code": 400, "message": "already exists"}}


class _RaceThenExistsSpreadsheet:
    """Simulates two concurrent get_or_create_tab callers.

    The first spreadsheet.worksheet(title) lookup raises NotFound (as if
    the tab genuinely didn't exist yet). add_worksheet then fails the way
    real Sheets fails when another caller won the race and created the
    tab first: gspread.exceptions.APIError. The second
    spreadsheet.worksheet(title) lookup then succeeds, returning the tab
    the other caller created.
    """

    def __init__(self, existing_ws):
        self._existing = existing_ws
        self._lookups = 0

    def worksheet(self, title):
        self._lookups += 1
        if self._lookups == 1:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._existing

    def add_worksheet(self, title, rows=100, cols=20):
        raise gspread.exceptions.APIError(_FakeApiErrorResponse())


def test_get_or_create_tab_survives_a_concurrent_create_race():
    """(w) Two callers both see WorksheetNotFound and both call
    add_worksheet; real Sheets accepts one and answers the other with
    gspread.exceptions.APIError. That must not escape as a raw gspread
    exception -- it should be treated as "someone else created it" and
    the existing tab returned.
    """
    existing = FakeWorksheet([LOG_HEADER], title=LOG_TAB)
    sh = _RaceThenExistsSpreadsheet(existing)
    result = get_or_create_tab(sh, LOG_TAB, LOG_HEADER)
    assert result is existing


def test_get_or_create_tab_raises_sheet_structure_error_when_the_race_never_resolves():
    class _AlwaysConflictingSpreadsheet:
        def worksheet(self, title):
            raise gspread.exceptions.WorksheetNotFound(title)

        def add_worksheet(self, title, rows=100, cols=20):
            raise gspread.exceptions.APIError(_FakeApiErrorResponse())

    with pytest.raises(SheetStructureError, match=LOG_TAB):
        get_or_create_tab(_AlwaysConflictingSpreadsheet(), LOG_TAB, LOG_HEADER)


def test_reversed_yes_is_reversed():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(attachment_id="aaa", image_sha256="hash-aaa"))
    row_number, entry = last_unreversed_entry(sh)
    mark_entry_reversed(sh, row_number, entry["attachment_id"])
    assert image_already_logged(sh, "hash-aaa") is False


def test_reversed_blank_is_not_reversed():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(image_sha256="hash-aaa", reversed=""))
    assert image_already_logged(sh, "hash-aaa") is True


def test_reversed_no_is_refused_rather_than_treated_as_falsy():
    """A human typing 'no' in the reversed column must not silently make
    image_already_logged fail open and let a re-posted screenshot be
    processed (and paid) a second time.
    """
    row = [
        "2026-08-06T21:00:00",
        "Week 17",
        "Lucus",
        "3",
        "111",
        "aaa",
        "hash-aaa",
        "officer#1",
        "Kobe, Talong",
        "no",
    ]
    sh = FakeSpreadsheet({LOG_TAB: FakeWorksheet([LOG_HEADER, row], title=LOG_TAB)})
    with pytest.raises(SheetStructureError, match="no"):
        image_already_logged(sh, "hash-aaa")
    with pytest.raises(SheetStructureError, match="no"):
        last_unreversed_entry(sh)


def test_a_cleared_row_of_all_blank_cells_is_skipped_not_returned_as_an_entry():
    """gspread pads a cleared row as all-empty-string cells, not `[]`. Such
    a row must not become an entry with every field blank -- if it were
    last, last_unreversed_entry would hand it back as "the thing to undo".
    """
    cleared_row = ["", "", "", "", "", "", "", "", "", ""]
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(boss="Lucus", attachment_id="aaa"))
    ws = sh.worksheet(LOG_TAB)
    ws._rows.append(cleared_row)

    row_number, entry = last_unreversed_entry(sh)
    assert entry["boss"] == "Lucus"
    assert row_number == 2


# -- Transient Sheets errors -------------------------------------------------
#
# Google answers a momentary backend problem with 503 "The service is
# currently unavailable" (and 500/502/504), and answers a shared-quota
# burst with 429. Neither says the request was wrong. attendance_sheet had
# no retry at all, so every one of those blips failed a member's command
# outright -- the same defect items_sheet had for 503 specifically.

def _api_error(code, message="boom"):
    response = type(
        "Response",
        (),
        {
            "status_code": code,
            "json": lambda self: {"error": {"code": code, "message": message}},
            "text": message,
        },
    )()
    error = gspread.exceptions.APIError(response)
    error.code = code
    return error


class FlakyWorksheet:
    """Fails `failures` times with `error`, then delegates to `inner`."""

    def __init__(self, inner, failures=0, error=None, append_failures=0):
        self._inner = inner
        self._remaining = failures
        # Counted separately from reads: a write path reads the header
        # first, so a shared budget would let that read swallow the
        # failure meant for the write.
        self._append_remaining = append_failures
        self._error = error or _api_error(503)
        self.read_attempts = 0
        self.append_attempts = 0

    def get_all_values(self):
        self.read_attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return self._inner.get_all_values()

    def append_row(self, row):
        self.append_attempts += 1
        if self._append_remaining > 0:
            self._append_remaining -= 1
            raise self._error
        return self._inner.append_row(row)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_a_read_retries_a_transient_error(code, monkeypatch):
    slept = []
    monkeypatch.setattr(attendance_sheet.time, "sleep", slept.append)
    inner = FakeWorksheet([["Player", "Points", "Venatus"], ["Kobe", "1", ""]])
    worksheet = FlakyWorksheet(inner, failures=2, error=_api_error(code))

    players = attendance_sheet.read_players(worksheet)

    assert players == ["Kobe"]
    assert worksheet.read_attempts == 3
    assert slept == list(attendance_sheet.RETRY_DELAYS)


@pytest.mark.parametrize("code", [403, 404, 400])
def test_a_read_does_not_retry_a_permanent_error(code, monkeypatch):
    """A revoked key or deleted sheet answers the same way every time."""
    monkeypatch.setattr(attendance_sheet.time, "sleep", lambda _: None)
    inner = FakeWorksheet([["Player", "Points"], ["Kobe", "1"]])
    worksheet = FlakyWorksheet(inner, failures=99, error=_api_error(code))

    with pytest.raises(gspread.exceptions.APIError):
        attendance_sheet.read_players(worksheet)

    assert worksheet.read_attempts == 1


def test_a_read_gives_up_after_the_last_retry(monkeypatch):
    monkeypatch.setattr(attendance_sheet.time, "sleep", lambda _: None)
    inner = FakeWorksheet([["Player", "Points"], ["Kobe", "1"]])
    worksheet = FlakyWorksheet(inner, failures=99)

    with pytest.raises(gspread.exceptions.APIError):
        attendance_sheet.read_players(worksheet)

    assert worksheet.read_attempts == len(attendance_sheet.RETRY_DELAYS) + 1


def test_a_write_is_never_retried(monkeypatch):
    """The safety rule items_sheet already follows, kept here too.

    A retried append would add the log row twice -- and unlike a read,
    the first attempt may well have succeeded before the error came back.
    """
    monkeypatch.setattr(attendance_sheet.time, "sleep", lambda _: None)
    inner = FakeWorksheet([attendance_sheet.LOG_HEADER])
    worksheet = FlakyWorksheet(inner, append_failures=1)
    spreadsheet = FakeSpreadsheet({attendance_sheet.LOG_TAB: worksheet})

    with pytest.raises(gspread.exceptions.APIError):
        attendance_sheet.append_log_entry(spreadsheet, {"Image SHA256": "abc"})

    assert worksheet.append_attempts == 1, "a write must not be repeated"
