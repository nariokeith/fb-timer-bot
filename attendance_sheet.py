"""Google Sheets access for the attendance log.

Cells are located by content, not by fixed coordinates: the row is the one
whose column A matches the player, the column is the one whose header
matches the boss. Reordering columns therefore breaks nothing.
"""

import json
import math

import gspread
import gspread.utils
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
    try:
        info = json.loads(service_account_json)
    except json.JSONDecodeError:
        # json.JSONDecodeError.doc holds the entire input verbatim -- which
        # here is the whole service-account credential, private key
        # included. `from None` drops the original exception (and its
        # .doc) from the traceback so it can never be exfiltrated by a
        # traceback-with-locals reporter.
        raise SheetStructureError("Service account JSON is not valid JSON") from None
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
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
    """1-based index of the column for this boss.

    Refuses a blank query outright -- an empty or whitespace-only name
    would otherwise match the first blank spacer header, silently
    directing writes into a column that isn't a boss at all. Also refuses
    when more than one header names the same boss (normal for a
    weekly sheet with a boss fought twice); guessing which occurrence the
    caller meant risks paying into the wrong week's column.
    """
    wanted = boss_name.strip()
    if not wanted:
        raise SheetStructureError("Cannot look up a blank boss name")
    wanted_cf = wanted.casefold()

    matches = []
    for index, cell in enumerate(read_headers(worksheet), start=1):
        base = header_base(cell)
        if not base:
            continue
        if base.casefold() == wanted_cf:
            matches.append(index)

    if not matches:
        raise SheetStructureError(
            f"No column for {boss_name!r} in worksheet {worksheet.title!r}"
        )
    if len(matches) > 1:
        columns = ", ".join(str(m) for m in matches)
        raise SheetStructureError(
            f"Multiple columns for {boss_name!r} in worksheet "
            f"{worksheet.title!r}: columns {columns}; refusing to guess"
        )
    return matches[0]


def _cell_number(row: list[str], column_index: int, cell_address: str) -> int:
    """Current value of a cell, treating blanks as 0.

    Anything else that isn't a plain whole number is refused rather than
    coerced: the guild owner confirmed boss columns only ever hold a whole
    number or a blank, so any other content (a note, an "x" marker, a
    formula, an out-of-range value like "inf", or a genuine fraction like
    "3.7") means the sheet's structure isn't what this code expects, and
    silently overwriting it with 0 + points would destroy it.

    "5.0" is accepted and treated as 5: Sheets sometimes renders a whole
    number with a trailing ".0", and that is still a whole number, not
    anomalous data -- refusing it would block a legitimate value for no
    safety benefit. A negative whole number (e.g. "-2", which shows up
    after an undo) parses normally too.
    """
    if column_index - 1 >= len(row):
        return 0
    raw = row[column_index - 1].strip()
    if not raw:
        return 0
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        raise SheetStructureError(
            f"Cell {cell_address} holds {raw!r}, which is not a number; "
            "refusing to overwrite it"
        )
    if math.isinf(value) or math.isnan(value):
        raise SheetStructureError(
            f"Cell {cell_address} holds {raw!r}, which is not a number; "
            "refusing to overwrite it"
        )
    if not value.is_integer():
        raise SheetStructureError(
            f"Cell {cell_address} holds {raw!r}, which is not a whole "
            "number; refusing to overwrite it"
        )
    return int(value)


def _rows_by_player(grid: list[list[str]], worksheet_title: str) -> dict[str, int]:
    """Row number (1-based) for each player, refusing a duplicate name.

    A hand-maintained sheet can end up with the same player in two rows
    (a re-add, a copy-paste). A dict comprehension keyed by name would
    silently let the later row win and leave the earlier row's total
    untouched with no error -- exactly the kind of silent data loss this
    module exists to prevent.
    """
    rows: dict[str, int] = {}
    for number, row in enumerate(grid, start=1):
        if number <= HEADER_ROW or not row:
            continue
        name = row[PLAYER_COLUMN - 1].strip()
        if not name:
            continue
        if name in rows:
            raise SheetStructureError(
                f"{name!r} has two rows ({rows[name]} and {number}) in "
                f"worksheet {worksheet_title!r}; refusing to guess"
            )
        rows[name] = number
    return rows


def plan_point_writes(
    worksheet, players: list[str], column_index: int, points: int
) -> list[dict]:
    """Build the batch payload that adds `points` for each player.

    `points` may be negative, which is how undo reverses a log. Raises
    rather than returning a partial payload if any player is missing -- a
    half-written attendance log is worse than none.

    Concurrency note: this function reads the current grid and computes
    absolute values to write; it does not lock anything. If two callers
    run plan_point_writes/apply_writes concurrently for the same cell,
    both read the same starting value and one write clobbers the other,
    silently losing an attendance entry. The caller (command layer) is
    responsible for serialising plan+apply pairs against each other; this
    module intentionally does not implement locking.
    """
    if column_index == POINTS_COLUMN:
        raise SheetStructureError("Refusing to write column B; it is a SUM formula")

    grid = _grid(worksheet)
    rows_by_player = _rows_by_player(grid, worksheet.title)

    missing = [p for p in players if p.strip() not in rows_by_player]
    if missing:
        raise SheetStructureError(
            f"No row for {', '.join(sorted(missing))} in "
            f"worksheet {worksheet.title!r}"
        )

    payload = []
    for player in players:
        row_number = rows_by_player[player.strip()]
        cell_address = gspread.utils.rowcol_to_a1(row_number, column_index)
        current = _cell_number(grid[row_number - 1], column_index, cell_address)
        payload.append(
            {
                "range": cell_address,
                "values": [[current + points]],
            }
        )
    return payload


def apply_writes(worksheet, payload: list[dict]) -> None:
    """Send every cell update as one request.

    The Sheets API allows 60 writes per minute per user; thirty players
    written individually would burn half that on a single command.

    Concurrency note: apply_writes sends the absolute values computed by
    plan_point_writes from a prior read. The caller must ensure no other
    plan_point_writes/apply_writes pair runs against the same worksheet in
    between, or one officer's write can silently overwrite another's --
    this module does not lock the worksheet itself.
    """
    if not payload:
        return
    worksheet.batch_update(payload)


CONFIG_TAB = "_BotConfig"
LOG_TAB = "_BotLog"

CONFIG_HEADER = ["key", "value"]
LOG_HEADER = [
    "timestamp",
    "tab",
    "boss",
    "points_each",
    "message_id",
    "attachment_id",
    "confirmed_by",
    "players",
    "reversed",
]


def get_or_create_tab(spreadsheet, title: str, header: list[str]):
    """Return the named worksheet, creating it with `header` if absent."""
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title, rows=1000, cols=len(header))
        worksheet.append_row(header)
        return worksheet


def read_config(spreadsheet) -> dict[str, str]:
    """Bot settings stored in the sheet itself.

    The sheet is used because Render wipes the disk on every restart, so
    a local file would not survive.
    """
    try:
        worksheet = spreadsheet.worksheet(CONFIG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return {}

    return {
        row[0].strip(): row[1].strip()
        for row in worksheet.get_all_values()[1:]
        if len(row) >= 2 and row[0].strip()
    }


def write_config(spreadsheet, key: str, value: str) -> None:
    """Set one config key, replacing any existing row for it."""
    worksheet = get_or_create_tab(spreadsheet, CONFIG_TAB, CONFIG_HEADER)

    for number, row in enumerate(worksheet.get_all_values(), start=1):
        if number > 1 and row and row[0].strip() == key:
            worksheet.update_cell(number, 2, value)
            return

    worksheet.append_row([key, value])


def append_log_entry(spreadsheet, entry: dict) -> None:
    """Record one confirmed submission in the audit tab."""
    worksheet = get_or_create_tab(spreadsheet, LOG_TAB, LOG_HEADER)
    worksheet.append_row([str(entry.get(field, "")) for field in LOG_HEADER])


def _log_rows(spreadsheet) -> list[tuple[int, dict]]:
    try:
        worksheet = spreadsheet.worksheet(LOG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return []

    rows = []
    for number, row in enumerate(worksheet.get_all_values(), start=1):
        if number == 1 or not row:
            continue
        padded = list(row) + [""] * (len(LOG_HEADER) - len(row))
        rows.append((number, dict(zip(LOG_HEADER, padded))))
    return rows


def attachment_already_logged(spreadsheet, attachment_id: str) -> bool:
    """True if this exact screenshot was logged and not later reversed."""
    return any(
        entry["attachment_id"] == attachment_id and not entry["reversed"].strip()
        for _, entry in _log_rows(spreadsheet)
    )


def last_unreversed_entry(spreadsheet) -> tuple[int, dict] | None:
    """Most recent log entry that has not been undone."""
    for number, entry in reversed(_log_rows(spreadsheet)):
        if not entry["reversed"].strip():
            return number, entry
    return None


def mark_entry_reversed(spreadsheet, row_number: int) -> None:
    """Flag a log entry as undone."""
    worksheet = spreadsheet.worksheet(LOG_TAB)
    worksheet.update_cell(row_number, LOG_HEADER.index("reversed") + 1, "yes")
