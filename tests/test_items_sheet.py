"""Tests for Logs Tracker access, against the shared gspread fakes."""

import pytest

import items_sheet
import items_rules
from attendance_sheet import SheetStructureError
from conftest import FakeSpreadsheet, FakeWorksheet

SPECIAL_GRID = [
    ["Player Name", "Asta's Heart", "Amentis' Foot"],
    ["Kobe", "TRUE", "FALSE"],
    ["Dajz", "FALSE", "FALSE"],
    ["chinchong ni Mumu", "FALSE", "FALSE"],
]

GEAR_GRID = [
    ["Player Name", "Asta's Belt", "Benji's Heart"],
    ["Kobe", "2", ""],
    ["Dajz", "", "1"],
    ["chinchong ni Mumu", "", ""],
]

LEDGER_GRID = [
    items_sheet.LEDGER_HEADER,
    ["2026-08-07 09:00:00", "Kobe", "Asta's Belt", "Gear", "Officer", "1", "aaa"],
]


def make_spreadsheet():
    return FakeSpreadsheet(
        {
            items_sheet.SPECIAL_TAB: FakeWorksheet(SPECIAL_GRID, title=items_sheet.SPECIAL_TAB),
            items_sheet.GEAR_TAB: FakeWorksheet(GEAR_GRID, title=items_sheet.GEAR_TAB),
            items_sheet.LEDGER_TAB: FakeWorksheet(LEDGER_GRID, title=items_sheet.LEDGER_TAB),
        }
    )


def test_snapshot_reads_roster_headers_and_ledger():
    snapshot = items_sheet.read_snapshot(make_spreadsheet())
    assert "chinchong ni Mumu" in snapshot.roster
    assert "Asta's Heart" in snapshot.special_headers
    assert "Asta's Belt" in snapshot.gear_headers
    assert snapshot.ledger_rows[0][1] == "Kobe"


def test_snapshot_excludes_the_ledger_header_row():
    snapshot = items_sheet.read_snapshot(make_spreadsheet())
    assert all(row[0] != "Timestamp (PHT)" for row in snapshot.ledger_rows)


def test_a_missing_gear_tab_yields_empty_gear_headers():
    spreadsheet = FakeSpreadsheet(
        {items_sheet.SPECIAL_TAB: FakeWorksheet(SPECIAL_GRID, title=items_sheet.SPECIAL_TAB)}
    )
    snapshot = items_sheet.read_snapshot(spreadsheet)
    assert snapshot.gear_headers == []
    assert snapshot.special_headers


def test_holds_special_reads_the_checkbox_from_the_snapshot():
    snapshot = items_sheet.read_snapshot(make_spreadsheet())
    assert items_sheet.holds_special(snapshot, "Kobe", "Asta's Heart") is True
    assert items_sheet.holds_special(snapshot, "Dajz", "Asta's Heart") is False


def test_holds_special_is_false_for_an_unknown_player_or_item():
    snapshot = items_sheet.read_snapshot(make_spreadsheet())
    assert items_sheet.holds_special(snapshot, "Nobody", "Asta's Heart") is False
    assert items_sheet.holds_special(snapshot, "Kobe", "No Such Item") is False


def test_holds_special_works_from_a_snapshot_alone():
    """The panel calls this once per line, so it must not re-read the sheet.

    Constructing the Snapshot by hand -- with no spreadsheet in reach --
    is what proves it: if the implementation reached for the API, there
    would be nothing here to reach for.
    """
    snapshot = items_sheet.Snapshot(
        roster=["Kobe"],
        special_headers=SPECIAL_GRID[0],
        gear_headers=[],
        ledger_rows=[],
        special_grid=SPECIAL_GRID,
    )
    assert items_sheet.holds_special(snapshot, "Kobe", "Asta's Heart") is True


def test_find_row_locates_a_player_case_insensitively():
    worksheet = FakeWorksheet(SPECIAL_GRID, title=items_sheet.SPECIAL_TAB)
    assert items_sheet.find_row(worksheet, "kobe") == 2
    assert items_sheet.find_row(worksheet, "chinchong ni Mumu") == 4


def test_find_row_refuses_an_unknown_player():
    worksheet = FakeWorksheet(SPECIAL_GRID, title=items_sheet.SPECIAL_TAB)
    with pytest.raises(SheetStructureError):
        items_sheet.find_row(worksheet, "Nobody")


def test_record_special_ticks_the_right_cell():
    spreadsheet = make_spreadsheet()
    address = items_sheet.record_special(spreadsheet, "Dajz", "Asta's Heart")

    payload = spreadsheet.worksheet(items_sheet.SPECIAL_TAB).batches[-1]
    assert payload == [{"range": "B3", "values": [[True]]}]
    assert address == "B3"


def test_record_special_refuses_when_already_ticked():
    spreadsheet = make_spreadsheet()
    with pytest.raises(SheetStructureError) as exc:
        items_sheet.record_special(spreadsheet, "Kobe", "Asta's Heart")
    assert "already" in str(exc.value).lower()
    assert spreadsheet.worksheet(items_sheet.SPECIAL_TAB).batches == []


def test_record_gear_increments_an_existing_count():
    spreadsheet = make_spreadsheet()
    items_sheet.record_gear(spreadsheet, "Kobe", "Asta's Belt")
    assert spreadsheet.worksheet(items_sheet.GEAR_TAB).batches[-1] == [
        {"range": "B2", "values": [[3]]}
    ]


def test_record_gear_treats_a_blank_cell_as_zero():
    spreadsheet = make_spreadsheet()
    items_sheet.record_gear(spreadsheet, "Dajz", "Asta's Belt")
    assert spreadsheet.worksheet(items_sheet.GEAR_TAB).batches[-1] == [
        {"range": "B3", "values": [[1]]}
    ]


def test_record_gear_refuses_a_non_numeric_cell_rather_than_overwriting():
    grid = [row[:] for row in GEAR_GRID]
    grid[1][1] = "n/a"
    spreadsheet = FakeSpreadsheet(
        {items_sheet.GEAR_TAB: FakeWorksheet(grid, title=items_sheet.GEAR_TAB)}
    )
    with pytest.raises(SheetStructureError) as exc:
        items_sheet.record_gear(spreadsheet, "Kobe", "Asta's Belt")
    assert "n/a" in str(exc.value)
    assert spreadsheet.worksheet(items_sheet.GEAR_TAB).batches == []


def test_record_gear_refuses_when_the_tab_is_missing():
    spreadsheet = FakeSpreadsheet(
        {items_sheet.SPECIAL_TAB: FakeWorksheet(SPECIAL_GRID, title=items_sheet.SPECIAL_TAB)}
    )
    with pytest.raises(SheetStructureError) as exc:
        items_sheet.record_gear(spreadsheet, "Kobe", "Asta's Belt")
    assert items_sheet.GEAR_TAB in str(exc.value)


def test_append_ledger_row_writes_the_columns_in_header_order():
    spreadsheet = make_spreadsheet()
    items_sheet.append_ledger_row(
        spreadsheet,
        timestamp="2026-08-07 14:00:00",
        ign="Dajz",
        item="Asta's Heart",
        item_type=items_rules.SPECIAL,
        officer="Keith",
        user_id=7,
        request_id="zzz",
    )
    assert spreadsheet.worksheet(items_sheet.LEDGER_TAB).appended[-1] == [
        "2026-08-07 14:00:00", "Dajz", "Asta's Heart", "Special", "Keith", "7", "zzz"
    ]


def test_append_ledger_row_creates_the_tab_when_absent():
    spreadsheet = FakeSpreadsheet(
        {items_sheet.SPECIAL_TAB: FakeWorksheet(SPECIAL_GRID, title=items_sheet.SPECIAL_TAB)}
    )
    items_sheet.append_ledger_row(
        spreadsheet,
        timestamp="2026-08-07 14:00:00",
        ign="Dajz",
        item="Asta's Heart",
        item_type=items_rules.SPECIAL,
        officer="Keith",
        user_id=7,
        request_id="zzz",
    )
    assert items_sheet.LEDGER_TAB in spreadsheet.created


def test_commit_approval_writes_the_cell_and_the_ledger_row():
    spreadsheet = make_spreadsheet()
    items_sheet.commit_approval(
        spreadsheet,
        ign="Dajz",
        item="Asta's Heart",
        item_type=items_rules.SPECIAL,
        timestamp="2026-08-07 14:00:00",
        officer="Keith",
        user_id=7,
        request_id="zzz",
    )
    assert spreadsheet.worksheet(items_sheet.SPECIAL_TAB).batches
    assert spreadsheet.worksheet(items_sheet.LEDGER_TAB).appended


def test_commit_approval_writes_no_ledger_row_when_the_cell_write_fails():
    spreadsheet = make_spreadsheet()
    with pytest.raises(SheetStructureError):
        items_sheet.commit_approval(
            spreadsheet,
            ign="Kobe",
            item="Asta's Heart",  # Kobe already has it
            item_type=items_rules.SPECIAL,
            timestamp="2026-08-07 14:00:00",
            officer="Keith",
            user_id=7,
            request_id="zzz",
        )
    assert spreadsheet.worksheet(items_sheet.LEDGER_TAB).appended == []


def test_a_failed_ledger_append_is_reported_as_its_own_unretryable_error(monkeypatch):
    spreadsheet = make_spreadsheet()

    def boom(*args, **kwargs):
        raise RuntimeError("ledger is down")

    monkeypatch.setattr(items_sheet, "append_ledger_row", boom)

    with pytest.raises(items_sheet.LedgerWriteError) as exc:
        items_sheet.commit_approval(
            spreadsheet,
            ign="Dajz",
            item="Asta's Heart",
            item_type=items_rules.SPECIAL,
            timestamp="2026-08-07 14:00:00",
            officer="Keith",
            user_id=7,
            request_id="zzz",
        )

    assert exc.value.address == "B3"
    assert exc.value.row[1] == "Dajz"
    assert spreadsheet.worksheet(items_sheet.SPECIAL_TAB).batches, (
        "the cell write already happened; the error must carry that fact"
    )
