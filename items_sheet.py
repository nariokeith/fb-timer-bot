"""Google Sheets access for the Logs Tracker spreadsheet.

Separate spreadsheet from the attendance sheet, but the same shape: row
1 holds item names, column A holds players, and the intersection is that
player's record. Cells are located by content, never by fixed
coordinates, so the user can keep adding Gear Logs columns while the bot
is running.
"""

import time
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

# Seconds to wait before each retry of a rate-limited read. Two waits,
# so the worst case a member feels is ~7 seconds before the answer
# arrives -- long enough to outlast a one-minute quota window's tail,
# short enough that Discord's own timeout is never in play.
RETRY_DELAYS = (2.0, 5.0)

# HTTP status Sheets returns when the per-minute read quota is spent.
RATE_LIMITED = 429

# Google's transient server-side refusals. A 503 ("The service is
# currently unavailable") is Sheets' standard backend blip and their own
# guidance is to retry it with backoff -- it says nothing about this
# request being wrong, only that Sheets could not serve it just now.
# Treating it as permanent put "APIError: [503]" in front of members
# running !request, for reads that would have succeeded a second later.
TRANSIENT_CODES = frozenset({RATE_LIMITED, 500, 502, 503, 504})


def _is_transient(exc: Exception) -> bool:
    """True if trying the same read again could plausibly give a different answer."""
    return (
        isinstance(exc, gspread.exceptions.APIError) and exc.code in TRANSIENT_CODES
    )


def _retrying_reads(read, sleep):
    """Run `read`, retrying while Sheets says "not now" rather than "no".

    Reads only, and deliberately so: this must never wrap a write.
    Retrying record_gear would increment a count twice, and record_special
    would refuse the second attempt outright -- see LedgerWriteError.

    "Not now" is a 429 (the two bots share one 60-per-minute credential)
    or one of Google's 5xx blips. A permanent APIError -- 403 on a
    revoked key, 404 on a deleted sheet -- fails the same way every
    time, so it is raised immediately rather than making the caller wait
    out the backoff for the same answer.
    """
    for delay in RETRY_DELAYS:
        try:
            return read()
        except Exception as exc:
            if not _is_transient(exc):
                raise
            sleep(delay)
    return read()


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


def _range_title(range_name: str) -> str:
    """The tab name out of an A1 range like "'Special Logs'!A1:C4".

    The API quotes a title only when it has to, so the response does not
    necessarily echo the range string that was sent. Unwrapping both
    forms here is what lets the caller match grids by name instead of
    trusting that the response arrives in the order requested.
    """
    title = range_name.rsplit("!", 1)[0] if "!" in range_name else range_name
    if len(title) >= 2 and title.startswith("'") and title.endswith("'"):
        title = title[1:-1].replace("''", "'")
    return title


def _read_grids(
    spreadsheet, sheets: dict, titles: tuple[str, ...]
) -> dict[str, list[list[str]]]:
    """Every named tab's grid in one API read, missing tabs omitted.

    `sheets` is the caller's already-paid-for title -> Worksheet map, so
    the only call here is values.batchGet: one read for all three grids.
    Resolving each tab with spreadsheet.worksheet() instead would spend a
    *separate* metadata fetch per tab -- gspread calls
    fetch_sheet_metadata() on every worksheet() -- which is what made one
    !request cost seven reads against a 60-per-minute quota shared with
    the attendance bot.

    Gear Logs is still being built. A missing tab is simply absent from
    the result, so the caller can degrade to "no gear items exist"
    rather than breaking special-log requests too.
    """
    wanted = [title for title in titles if title in sheets]
    if not wanted:
        return {}
    response = spreadsheet.values_batch_get(
        [gspread.utils.absolute_range_name(title) for title in wanted]
    )
    returned = {
        _range_title(value_range.get("range", "")): value_range
        for value_range in response.get("valueRanges", [])
    }
    grids = {}
    for title in wanted:
        value_range = returned.get(title)
        if value_range is None:
            grids[title] = []
            continue
        values = value_range.get("values")
        # An absent tab and a present-but-empty one both mean "nothing to
        # read here", so both become []. get_all_values would have said
        # [[]] for the empty sheet -- a truthy value that would slip past
        # the caller's "missing or empty" guard.
        grids[title] = gspread.utils.fill_gaps(values) if values else []
    return grids


def read_snapshot(spreadsheet, sleep=time.sleep) -> Snapshot:
    """Everything a request decision needs, read in one pass.

    One snapshot per command rather than a read per question: the Sheets
    API allows 60 reads per minute per user, and every question here
    (roster, both header rows, the ledger) would otherwise be its own
    call.

    Retries itself on a quota refusal, so a burst from the attendance bot
    -- which shares the credential and therefore the quota -- costs a
    member a few seconds rather than an error embed. `sleep` is injected
    only so tests need not actually wait.
    """
    return _retrying_reads(lambda: _read_snapshot_once(spreadsheet), sleep)


def _read_snapshot_once(spreadsheet) -> Snapshot:
    # One metadata fetch, reused for both "which tabs exist" and the
    # Worksheet handle read_players/read_headers name in their errors.
    sheets = {sheet.title: sheet for sheet in spreadsheet.worksheets()}
    grids = _read_grids(spreadsheet, sheets, (SPECIAL_TAB, GEAR_TAB, LEDGER_TAB))
    special_grid = grids.get(SPECIAL_TAB, [])
    if not special_grid:
        raise SheetStructureError(f"Worksheet {SPECIAL_TAB!r} is missing or empty")
    gear_grid = grids.get(GEAR_TAB, [])
    ledger_grid = grids.get(LEDGER_TAB, [])
    if ledger_grid and ledger_grid[HEADER_ROW - 1] != LEDGER_HEADER:
        raise SheetStructureError(
            f"Worksheet {LEDGER_TAB!r} has an unexpected header "
            f"{ledger_grid[HEADER_ROW - 1]!r}; expected {LEDGER_HEADER!r}. "
            "Refusing to guess ledger column positions."
        )

    special = sheets[SPECIAL_TAB]
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
    matches = []
    for index, row in enumerate(grid[HEADER_ROW:], start=HEADER_ROW + 1):
        if not row:
            continue
        if normalize(row[PLAYER_COLUMN - 1]) == wanted:
            matches.append(index)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise SheetStructureError(
            f"Multiple rows for {ign!r} in worksheet {worksheet.title!r}: "
            f"{', '.join(map(str, matches))}; refusing to guess"
        )
    raise SheetStructureError(
        f"No row for {ign!r} in worksheet {worksheet.title!r}"
    )


def already_recorded(snapshot: Snapshot, request_id: str) -> bool:
    """Whether the ledger already contains this request's audit row."""
    return any(len(row) > 6 and row[6] == request_id for row in snapshot.ledger_rows)


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


class AlreadyHeld(SheetStructureError):
    """This player's checkbox for this special log is already ticked.

    Its own type because callers act on it differently from every other
    structural problem: nothing was written and nothing needs writing,
    the record is simply already correct. Recognising that by matching
    words in an error message would be wrong -- record_special quotes
    the offending cell's contents into its other messages, so a cell
    holding the wrong text could imitate any phrase chosen here.
    """


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

    raw = _cell(grid, row, column).strip()
    state = raw.casefold()
    if state not in {"", "false", "true"}:
        raise SheetStructureError(
            f"Cell for {ign} / {item!r} holds {raw!r}, which is not a checkbox "
            "state; refusing to overwrite a value this bot does not understand"
        )
    if state in CHECKED_VALUES:
        raise AlreadyHeld(
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
