"""Google Sheets access for the Logs Tracker spreadsheet.

Separate spreadsheet from the attendance sheet, but the same shape: row
1 holds item names, column A holds players, and the intersection is that
player's record. Cells are located by content, never by fixed
coordinates, so the user can keep adding Gear Logs columns while the bot
is running.
"""

from dataclasses import dataclass

import gspread
import gspread.utils

from attendance_bosses import header_base
from attendance_roster import normalize
from attendance_sheet import (
    HEADER_ROW,
    PLAYER_COLUMN,
    SheetStructureError,
    find_column,
    get_or_create_tab,
    open_spreadsheet,
    read_headers,
    read_players,
)
import items_rules

SPECIAL_TAB = "Special Logs"
GEAR_TAB = "Gear Logs"
LEDGER_TAB = "Distribution Log"

LEDGER_HEADER = [
    "Timestamp (PHT)",
    "IGN",
    "Item",
    "Type",
    "Officer",
    "Discord User ID",
    "Request ID",
]

# Google Sheets renders a checked checkbox as this in get_all_values().
CHECKED_VALUES = {"true"}


def open_logs_tracker(sheet_id: str, service_account_json: str):
    return open_spreadsheet(sheet_id, service_account_json)


@dataclass(frozen=True)
class Snapshot:
    """Everything a decision needs, from one read of the spreadsheet.

    special_grid is carried whole rather than reduced to headers,
    because the checkbox values in it answer "does this player already
    hold this special log" -- a question asked once per !request and
    once per line of every !distribute panel. Reading it once here keeps
    those off the Sheets API entirely.
    """

    roster: list[str]
    special_headers: list[str]
    gear_headers: list[str]
    ledger_rows: list[list[str]]
    special_grid: list[list[str]]


def _grid_or_empty(spreadsheet, title: str) -> list[list[str]]:
    """The tab's full grid, or [] if the tab does not exist yet.

    Gear Logs is still being built. A missing tab must degrade to "no
    gear items exist" rather than breaking special-log requests too.
    """
    try:
        return spreadsheet.worksheet(title).get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        return []


def read_snapshot(spreadsheet) -> Snapshot:
    """Everything a request decision needs, read in one pass.

    One snapshot per command rather than a read per question: the Sheets
    API allows 60 reads per minute per user, and every question here
    (roster, both header rows, the ledger) would otherwise be its own
    call.
    """
    special_grid = _grid_or_empty(spreadsheet, SPECIAL_TAB)
    if not special_grid:
        raise SheetStructureError(f"Worksheet {SPECIAL_TAB!r} is missing or empty")
    gear_grid = _grid_or_empty(spreadsheet, GEAR_TAB)
    ledger_grid = _grid_or_empty(spreadsheet, LEDGER_TAB)

    special = spreadsheet.worksheet(SPECIAL_TAB)
    return Snapshot(
        roster=read_players(special, special_grid),
        special_headers=read_headers(special, special_grid),
        gear_headers=list(gear_grid[HEADER_ROW - 1]) if gear_grid else [],
        ledger_rows=ledger_grid[HEADER_ROW:] if ledger_grid else [],
        special_grid=special_grid,
    )


def find_row(worksheet, ign: str, grid: list[list[str]] | None = None) -> int:
    """1-based row index for this player in this tab.

    Refuses rather than creating a row: the sheet is hand-maintained and
    a bot-invented row would be invisible to the officers who curate it.
    """
    grid = grid if grid is not None else worksheet.get_all_values()
    wanted = normalize(ign)
    for index, row in enumerate(grid[HEADER_ROW:], start=HEADER_ROW + 1):
        if not row:
            continue
        if normalize(row[PLAYER_COLUMN - 1]) == wanted:
            return index
    raise SheetStructureError(
        f"No row for {ign!r} in worksheet {worksheet.title!r}"
    )


def holds_special(snapshot: Snapshot, ign: str, item: str) -> bool:
    """Whether this player's checkbox for this special log is ticked.

    Pure: it reads the grid already in the snapshot rather than calling
    the API again. An unknown player or item is False, not an error --
    callers that care about existence have already resolved both through
    items_rules, and the panel must be able to render a line for a
    request whose column was renamed since it was queued.
    """
    wanted_player = normalize(ign)
    wanted_item = normalize(item)

    column = None
    if snapshot.special_grid:
        for index, cell in enumerate(snapshot.special_grid[HEADER_ROW - 1], start=1):
            if normalize(header_base(cell)) == wanted_item:
                column = index
                break
    if column is None:
        return False

    for row in snapshot.special_grid[HEADER_ROW:]:
        if not row or normalize(row[PLAYER_COLUMN - 1]) != wanted_player:
            continue
        if len(row) < column:
            return False
        return row[column - 1].strip().casefold() in CHECKED_VALUES
    return False
