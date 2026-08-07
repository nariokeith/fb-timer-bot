"""Tests for Logs Tracker access, against the shared gspread fakes."""

import pytest

import items_sheet
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
