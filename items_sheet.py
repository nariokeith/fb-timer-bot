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


class LedgerWriteError(RuntimeError):
    """The item cell was written but the ledger row was not.

    This is NOT retryable, which is why it is its own type. Retrying
    would increment a gear count a second time, and a special-log retry
    could never succeed at all -- record_special refuses a checkbox that
    is now ticked. The caller must drop the request and tell the
    officers exactly which row to add by hand.
    """

    def __init__(self, address: str, row: list[str], cause: Exception):
        super().__init__(
            f"Wrote {address} but could not append the ledger row: {cause}"
        )
        self.address = address
        self.row = row


def _worksheet_or_refuse(spreadsheet, title: str):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        raise SheetStructureError(
            f"Worksheet {title!r} does not exist in this spreadsheet yet"
        ) from None


def _cell(grid: list[list[str]], row: int, column: int) -> str:
    cells = grid[row - 1] if len(grid) >= row else []
    return cells[column - 1] if len(cells) >= column else ""


def record_special(spreadsheet, ign: str, item: str) -> str:
    """Tick this player's checkbox. Returns the A1 address written.

    Refuses if it is already ticked. That is the special-log rule
    enforced at the last possible moment -- the officer's click may be
    minutes after the request was queued, and the sheet may have been
    edited by hand in between.
    """
    worksheet = _worksheet_or_refuse(spreadsheet, SPECIAL_TAB)
    grid = worksheet.get_all_values()
    row = find_row(worksheet, ign, grid)
    column = find_column(worksheet, item, grid)

    if _cell(grid, row, column).strip().casefold() in CHECKED_VALUES:
        raise SheetStructureError(
            f"{ign} already has {item!r} -- a special log is once only"
        )

    address = gspread.utils.rowcol_to_a1(row, column)
    worksheet.batch_update([{"range": address, "values": [[True]]}])
    return address


def record_gear(spreadsheet, ign: str, item: str) -> str:
    """Add one to this player's count. Returns the A1 address written."""
    worksheet = _worksheet_or_refuse(spreadsheet, GEAR_TAB)
    grid = worksheet.get_all_values()
    row = find_row(worksheet, ign, grid)
    column = find_column(worksheet, item, grid)

    raw = _cell(grid, row, column).strip()
    if not raw:
        current = 0
    else:
        try:
            current = int(raw)
        except ValueError:
            raise SheetStructureError(
                f"Cell for {ign} / {item!r} holds {raw!r}, which is not a "
                "number; refusing to overwrite a value this bot does not "
                "understand"
            ) from None

    address = gspread.utils.rowcol_to_a1(row, column)
    worksheet.batch_update([{"range": address, "values": [[current + 1]]}])
    return address


def append_ledger_row(
    spreadsheet,
    *,
    timestamp: str,
    ign: str,
    item: str,
    item_type: str,
    officer: str,
    user_id: int,
    request_id: str,
) -> None:
    """Append one audit row, creating the tab on first use."""
    worksheet = get_or_create_tab(spreadsheet, LEDGER_TAB, LEDGER_HEADER)
    worksheet.append_row(
        [timestamp, ign, item, item_type, officer, str(user_id), request_id]
    )


def commit_approval(
    spreadsheet,
    *,
    ign: str,
    item: str,
    item_type: str,
    timestamp: str,
    officer: str,
    user_id: int,
    request_id: str,
) -> str:
    """Write the item cell, then the ledger row. Returns the cell address.

    Cell first, ledger second, deliberately. If the cell write fails
    there must be no ledger row, or the daily cap would count an item
    the player never received. The reverse gap (cell written, ledger
    append fails) undercounts instead, which is recoverable by hand and
    never denies anyone an item they are owed.
    """
    if item_type == items_rules.SPECIAL:
        address = record_special(spreadsheet, ign, item)
    elif item_type == items_rules.GEAR:
        address = record_gear(spreadsheet, ign, item)
    else:
        raise SheetStructureError(f"Unknown item type {item_type!r}")

    row = [timestamp, ign, item, item_type, officer, str(user_id), request_id]
    try:
        append_ledger_row(
            spreadsheet,
            timestamp=timestamp,
            ign=ign,
            item=item,
            item_type=item_type,
            officer=officer,
            user_id=user_id,
            request_id=request_id,
        )
    except Exception as exc:
        raise LedgerWriteError(address, row, exc) from exc
    return address
