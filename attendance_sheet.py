"""Google Sheets access for the attendance log.

Cells are located by content, not by fixed coordinates: the row is the one
whose column A matches the player, the column is the one whose header
matches the boss. Reordering columns therefore breaks nothing.
"""

import json

import gspread
from google.oauth2.service_account import Credentials

from attendance_bosses import header_base

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADER_ROW = 1
PLAYER_COLUMN = 1
POINTS_COLUMN = 2  # a SUM formula -- never written


class SheetStructureError(RuntimeError):
    """The sheet does not look the way the bot needs it to."""


def open_spreadsheet(sheet_id: str, service_account_json: str):
    """Authorise with a service account and open the spreadsheet by ID."""
    creds = Credentials.from_service_account_info(
        json.loads(service_account_json), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(sheet_id)


def _grid(worksheet) -> list[list[str]]:
    return worksheet.get_all_values()


def read_headers(worksheet) -> list[str]:
    """The header row, verbatim."""
    grid = _grid(worksheet)
    if not grid:
        raise SheetStructureError(f"Worksheet {worksheet.title!r} is empty")
    return list(grid[HEADER_ROW - 1])


def read_players(worksheet) -> list[str]:
    """Player names in column A, below the header row."""
    return [
        row[PLAYER_COLUMN - 1].strip()
        for row in _grid(worksheet)[HEADER_ROW:]
        if row and row[PLAYER_COLUMN - 1].strip()
    ]


def find_column(worksheet, boss_name: str) -> int:
    """1-based index of the column for this boss."""
    wanted = boss_name.strip().casefold()
    for index, cell in enumerate(read_headers(worksheet), start=1):
        if header_base(cell).casefold() == wanted:
            return index
    raise SheetStructureError(
        f"No column for {boss_name!r} in worksheet {worksheet.title!r}"
    )


def _cell_number(row: list[str], column_index: int) -> int:
    """Current value of a cell, treating blanks and junk as 0."""
    if column_index - 1 >= len(row):
        return 0
    raw = row[column_index - 1].strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def plan_point_writes(
    worksheet, players: list[str], column_index: int, points: int
) -> list[dict]:
    """Build the batch payload that adds `points` for each player.

    `points` may be negative, which is how undo reverses a log. Raises
    rather than returning a partial payload if any player is missing -- a
    half-written attendance log is worse than none.
    """
    if column_index == POINTS_COLUMN:
        raise SheetStructureError("Refusing to write column B; it is a SUM formula")

    grid = _grid(worksheet)
    rows_by_player = {
        row[PLAYER_COLUMN - 1].strip(): number
        for number, row in enumerate(grid, start=1)
        if number > HEADER_ROW and row and row[PLAYER_COLUMN - 1].strip()
    }

    missing = [p for p in players if p not in rows_by_player]
    if missing:
        raise SheetStructureError(
            f"No row for {', '.join(sorted(missing))} in "
            f"worksheet {worksheet.title!r}"
        )

    payload = []
    for player in players:
        row_number = rows_by_player[player]
        current = _cell_number(grid[row_number - 1], column_index)
        payload.append(
            {
                "range": gspread.utils.rowcol_to_a1(row_number, column_index),
                "values": [[current + points]],
            }
        )
    return payload


def apply_writes(worksheet, payload: list[dict]) -> None:
    """Send every cell update as one request.

    The Sheets API allows 60 writes per minute per user; thirty players
    written individually would burn half that on a single command.
    """
    if not payload:
        return
    worksheet.batch_update(payload)
