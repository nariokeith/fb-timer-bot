"""Tests for what the preflight check reports about the sheet's settings.

Preflight is how an officer confirms an edit to _BotConfig landed without
starting the bot or asking Discord. That only helps if it reports the
number the bot will actually enforce -- a preflight that echoed
ITEMS_GEAR_DAILY_CAP while the bot obeyed the sheet would be worse than
no report at all.
"""

import items_preflight
import items_sheet
from conftest import FakeSpreadsheet, FakeWorksheet
from test_items_sheet import GEAR_GRID, LEDGER_GRID, SPECIAL_GRID
from attendance_sheet import CONFIG_HEADER


def spreadsheet_with(config_rows):
    return FakeSpreadsheet(
        {
            items_sheet.SPECIAL_TAB: FakeWorksheet(SPECIAL_GRID, title=items_sheet.SPECIAL_TAB),
            items_sheet.GEAR_TAB: FakeWorksheet(GEAR_GRID, title=items_sheet.GEAR_TAB),
            items_sheet.LEDGER_TAB: FakeWorksheet(LEDGER_GRID, title=items_sheet.LEDGER_TAB),
            items_sheet.CONFIG_TAB: FakeWorksheet(
                [CONFIG_HEADER, *config_rows], title=items_sheet.CONFIG_TAB
            ),
        }
    )


def test_preflight_reports_the_cap_the_sheet_sets_not_the_environments(monkeypatch, capsys):
    monkeypatch.setenv("ITEMS_GEAR_DAILY_CAP", "5")

    items_preflight.check_snapshot(spreadsheet_with([["gear_daily_cap", "2"]]))

    out = capsys.readouterr().out
    assert "gear cap 2" in out
    assert "gear cap 5" not in out


def test_preflight_says_where_the_cap_came_from_when_the_sheet_has_no_row(monkeypatch, capsys):
    monkeypatch.setenv("ITEMS_GEAR_DAILY_CAP", "5")

    items_preflight.check_snapshot(spreadsheet_with([["officer_channel_id", "42"]]))

    out = capsys.readouterr().out
    assert "gear cap 5" in out
    assert "ITEMS_GEAR_DAILY_CAP" in out


def test_preflight_warns_about_a_cap_cell_the_bot_will_ignore(monkeypatch, capsys):
    """A typo in the cell must not read as a successful edit.

    Without the warning, an officer who typed "three" sees a clean
    preflight and believes the cap is three when the bot is still on five.
    """
    monkeypatch.setenv("ITEMS_GEAR_DAILY_CAP", "5")

    items_preflight.check_snapshot(spreadsheet_with([["gear_daily_cap", "three"]]))

    out = capsys.readouterr().out
    assert items_preflight.WARN in out
    assert "gear cap 5" in out
