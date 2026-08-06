import pytest

from attendance_sheet import (
    SheetStructureError,
    apply_writes,
    find_column,
    plan_point_writes,
    read_headers,
    read_players,
)
from conftest import SAMPLE_GRID, FakeWorksheet


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
    assert plan_point_writes(ws, ["Kobe"], 6, -2) == [
        {"range": "F4", "values": [[0]]}
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


from attendance_sheet import (
    CONFIG_TAB,
    LOG_HEADER,
    LOG_TAB,
    append_log_entry,
    attachment_already_logged,
    get_or_create_tab,
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
    append_log_entry(sh, _entry(attachment_id="aaa"))
    assert attachment_already_logged(sh, "aaa") is True
    assert attachment_already_logged(sh, "bbb") is False


def test_reversed_entries_do_not_count_as_already_logged():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(attachment_id="aaa"))
    row_number, _ = last_unreversed_entry(sh)
    mark_entry_reversed(sh, row_number)
    assert attachment_already_logged(sh, "aaa") is False


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
    mark_entry_reversed(sh, 3)
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
    assert entry["confirmed_by"] == ""
    assert entry["players"] == ""
    assert entry["reversed"] == ""
