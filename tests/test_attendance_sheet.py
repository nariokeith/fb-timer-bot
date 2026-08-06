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
