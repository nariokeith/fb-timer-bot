"""Google Sheets access for the attendance log.

Cells are located by content, not by fixed coordinates: the row is the one
whose column A matches the player, the column is the one whose header
matches the boss. Reordering columns therefore breaks nothing.
"""

import json
import math
import time

import gspread
import gspread.utils
from google.oauth2.service_account import Credentials

from attendance_bosses import header_base

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADER_ROW = 1
PLAYER_COLUMN = 1
POINTS_COLUMN = 2  # a SUM formula -- never written

# gspread defaults to NO request timeout: a single black-holed TCP
# connection would hang a gspread call forever, which -- since every call
# happens inside _SHEET_LOCK -- wedges the lock permanently and every
# later command hangs silently on "Working on it...". 15s is generous for
# a Sheets API call (which normally completes in well under a second) but
# still short enough that a stuck connection surfaces as a visible error
# instead of an indefinite hang.
REQUEST_TIMEOUT = 15.0


class SheetStructureError(RuntimeError):
    """The sheet does not look the way the bot needs it to."""


RETRY_DELAYS = (2.0, 5.0)

# Sheets answers a momentary backend problem with 500/502/503/504 -- 503
# is "The service is currently unavailable" -- and a shared-quota burst
# with 429. None of them says the request was wrong, only that Sheets
# could not serve it just now, and Google's own guidance is to retry them
# with backoff. Anything else (403 on a revoked key, 404 on a deleted
# sheet) answers identically every time and is raised at once, so a
# genuinely broken setup costs nobody a wait.
TRANSIENT_CODES = frozenset({429, 500, 502, 503, 504})


def is_transient(exc: Exception) -> bool:
    """True if repeating the same call could plausibly give a different answer."""
    return isinstance(exc, gspread.exceptions.APIError) and exc.code in TRANSIENT_CODES


def retrying_read(call, sleep=None):
    """Run `call`, retrying it while Sheets says "not now" rather than "no".

    READS ONLY. A write must never be routed through here: a repeated
    append_row adds the row twice, and the first attempt may well have
    succeeded before the error came back -- an attendance entry logged
    twice is worse than one that visibly failed. The write paths in this
    module (apply_writes, append_log_entry, write_config, add_worksheet)
    deliberately call gspread directly.
    """
    if sleep is None:
        sleep = time.sleep
    for delay in RETRY_DELAYS:
        try:
            return call()
        except Exception as exc:
            if not is_transient(exc):
                raise
            sleep(delay)
    return call()


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
    client = gspread.authorize(creds)
    client.set_timeout(REQUEST_TIMEOUT)
    return client.open_by_key(sheet_id)


def _grid(worksheet) -> list[list[str]]:
    """The one read primitive; every read path in this module goes through it."""
    return retrying_read(worksheet.get_all_values)


def _worksheet(spreadsheet, title):
    """Look a tab up, retrying a transient refusal.

    gspread refetches the spreadsheet metadata on every worksheet() call,
    so this is a real HTTP round trip and can fail the same way a read of
    the cells can. WorksheetNotFound is not an APIError, so it still
    propagates immediately for the callers that branch on it.
    """
    return retrying_read(lambda: spreadsheet.worksheet(title))


def read_headers(worksheet, grid: list[list[str]] | None = None) -> list[str]:
    """The header row, verbatim.

    `grid` lets a caller that already fetched the full grid (e.g.
    _load_context, which otherwise reads the same sheet three times) pass
    it in instead of triggering another get_all_values() call. Omit it to
    fetch fresh, as every existing caller does.
    """
    grid = grid if grid is not None else _grid(worksheet)
    if not grid:
        raise SheetStructureError(f"Worksheet {worksheet.title!r} is empty")
    return list(grid[HEADER_ROW - 1])


def read_players(worksheet, grid: list[list[str]] | None = None) -> list[str]:
    """Player names in column A, below the header row.

    See read_headers for what `grid` is for.
    """
    grid = grid if grid is not None else _grid(worksheet)
    return [
        row[PLAYER_COLUMN - 1].strip()
        for row in grid[HEADER_ROW:]
        if row and row[PLAYER_COLUMN - 1].strip()
    ]


def find_column(
    worksheet, boss_name: str, grid: list[list[str]] | None = None
) -> int:
    """1-based index of the column for this boss.

    Refuses a blank query outright -- an empty or whitespace-only name
    would otherwise match the first blank spacer header, silently
    directing writes into a column that isn't a boss at all. Also refuses
    when more than one header names the same boss (normal for a
    weekly sheet with a boss fought twice); guessing which occurrence the
    caller meant risks paying into the wrong week's column.

    See read_headers for what `grid` is for.
    """
    wanted = boss_name.strip()
    if not wanted:
        raise SheetStructureError("Cannot look up a blank boss name")
    wanted_cf = wanted.casefold()

    matches = []
    for index, cell in enumerate(read_headers(worksheet, grid), start=1):
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
    worksheet,
    players: list[str],
    column_index: int,
    points: int,
    grid: list[list[str]] | None = None,
) -> list[dict]:
    """Build the batch payload that adds `points` for each player.

    `points` may be negative, which is how undo reverses a log. Raises
    rather than returning a partial payload if any player is missing -- a
    half-written attendance log is worse than none. When the computed
    result for a cell is exactly 0, the payload writes an empty string
    there instead of the integer 0, so a cell that nets back to nothing
    (e.g. after an undo) reads as blank again rather than a visible zero.

    `grid` lets a caller that already fetched the full grid in this same
    critical section (e.g. _commit, which also needs it to resolve the
    boss column) pass it in instead of reading the sheet a second time.
    Omit it to fetch fresh, as every existing caller does -- this does NOT
    weaken the freshness guarantee inside the lock: a caller that wants a
    fresh read still gets one, it just reads once instead of twice.

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

    grid = grid if grid is not None else _grid(worksheet)
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
        new_value = current + points
        # A result of exactly 0 is written back as an empty string, not the
        # integer 0. Arithmetic doesn't care -- column B's SUM treats them
        # identically -- but a cell that was blank before this row's history
        # began (never boss-attended, or later undone back to nothing)
        # should read as blank again rather than gradually speckling a
        # hand-maintained sheet with visible zeros over time.
        payload.append(
            {
                "range": cell_address,
                "values": [[new_value if new_value != 0 else ""]],
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
    "attachment_id",  # per-upload Discord id; kept only for row identity
                       # (mark_entry_reversed's re-check), NOT for duplicate
                       # detection -- re-posting the same image mints a new
                       # attachment_id, so it can never catch a real repost.
    "image_sha256",  # hash of the image bytes; this is what duplicate
                      # detection actually keys on, because it is the same
                      # every time the same picture is posted.
    "confirmed_by",
    "players",
    "reversed",
]


REVERSED_TRUE_VALUES = {"yes", "true", "1"}


def _expect_header(
    worksheet, header: list[str], grid: list[list[str]] | None = None
) -> None:
    """Raise unless the tab's own row 1 equals the header this code expects.

    Row 1 is the ground truth for what order each column holds; it is
    written once, at tab creation, by get_or_create_tab. If LOG_HEADER (or
    CONFIG_HEADER) is later reordered by a code change, a live tab's row 1
    still holds the OLD order -- and every row already in it was written
    under that OLD order too. Zipping those rows against a NEW in-code
    header without checking would silently misassign every field (e.g.
    attachment_id read out of the confirmed_by column), and undo would
    subtract points from the wrong people with no error at all. Raising
    here -- on both the write path (get_or_create_tab) and the read path
    (_log_rows) -- turns that into a loud failure instead of a silent one,
    consistent with this module's refuse-rather-than-guess convention for
    duplicate player rows, duplicate boss columns, and non-numeric cells.
    """
    grid = grid if grid is not None else _grid(worksheet)
    if not grid:
        raise SheetStructureError(f"Worksheet {worksheet.title!r} is empty")
    actual = list(grid[0])
    if actual != header:
        raise SheetStructureError(
            f"Worksheet {worksheet.title!r} row 1 is {actual!r}, but this "
            f"code expects {header!r}; refusing to guess at field order"
        )


def get_or_create_tab(spreadsheet, title: str, header: list[str]):
    """Return the named worksheet, creating it with `header` if absent.

    Raises SheetStructureError if an existing tab's row 1 does not match
    `header` -- see _expect_header.

    Concurrency note: two callers can both see WorksheetNotFound and both
    call add_worksheet for the same title. Real Sheets accepts only one
    and answers the other with gspread.exceptions.APIError (a
    duplicate-title conflict). That is caught here and treated as "someone
    else just created it": the tab is looked up again and used. This makes
    the create race safe without the caller doing anything; it does NOT
    make concurrent writes to the tab's contents safe -- that remains the
    caller's responsibility, same as plan_point_writes/apply_writes.
    """
    try:
        worksheet = _worksheet(spreadsheet, title)
    except gspread.exceptions.WorksheetNotFound:
        try:
            worksheet = spreadsheet.add_worksheet(title, rows=1000, cols=len(header))
            worksheet.append_row(header)
            return worksheet
        except gspread.exceptions.APIError as exc:
            try:
                worksheet = _worksheet(spreadsheet, title)
            except gspread.exceptions.WorksheetNotFound:
                raise SheetStructureError(
                    f"Could not create or find worksheet {title!r}: {exc}"
                ) from exc

    _expect_header(worksheet, header)
    return worksheet


def read_config(spreadsheet) -> dict[str, str]:
    """Bot settings stored in the sheet itself.

    The sheet is used because Render wipes the disk on every restart, so
    a local file would not survive.

    Raises SheetStructureError if the same key appears in two rows.
    Silently letting the later row win (a plain dict comprehension would)
    could read back a different value than write_config just wrote for the
    same key -- exactly the silent-disagreement failure mode this module
    exists to prevent, and it is what write_config now refuses too.
    """
    try:
        worksheet = _worksheet(spreadsheet, CONFIG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return {}

    seen: dict[str, int] = {}
    result: dict[str, str] = {}
    for number, row in enumerate(_grid(worksheet), start=1):
        if number == 1 or not row:
            continue
        key = row[0].strip()
        if not key:
            continue
        if key in seen:
            raise SheetStructureError(
                f"{key!r} has two rows ({seen[key]} and {number}) in "
                f"worksheet {worksheet.title!r}; refusing to guess"
            )
        seen[key] = number
        result[key] = row[1].strip() if len(row) >= 2 else ""
    return result


def write_config(spreadsheet, key: str, value: str) -> None:
    """Set one config key, replacing any existing row for it.

    Raises SheetStructureError if the key is already duplicated in the
    sheet, matching read_config's own refusal -- so a successful write can
    never be silently read back as some other row's value. The incoming
    key is stripped before comparison (matching read_config, which strips
    both key and value) so that a key with stray whitespace cannot append
    a row no later write can ever match, which would itself manufacture a
    duplicate.
    """
    key = key.strip()
    worksheet = get_or_create_tab(spreadsheet, CONFIG_TAB, CONFIG_HEADER)

    seen: dict[str, int] = {}
    match_row = None
    for number, row in enumerate(_grid(worksheet), start=1):
        if number == 1 or not row:
            continue
        row_key = row[0].strip()
        if not row_key:
            continue
        if row_key in seen:
            raise SheetStructureError(
                f"{row_key!r} has two rows ({seen[row_key]} and {number}) "
                f"in worksheet {worksheet.title!r}; refusing to guess"
            )
        seen[row_key] = number
        if row_key == key:
            match_row = number

    if match_row is not None:
        worksheet.update_cell(match_row, 2, value)
        return

    worksheet.append_row([key, value])


def append_log_entry(spreadsheet, entry: dict) -> None:
    """Record one confirmed submission in the audit tab."""
    worksheet = get_or_create_tab(spreadsheet, LOG_TAB, LOG_HEADER)
    worksheet.append_row([str(entry.get(field, "")) for field in LOG_HEADER])


def _log_rows(spreadsheet) -> list[tuple[int, dict]]:
    try:
        worksheet = _worksheet(spreadsheet, LOG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return []

    # One read, reused for both the header check and the rows -- the
    # previous version called get_all_values() twice for every single
    # duplicate-detection or undo lookup.
    grid = _grid(worksheet)
    _expect_header(worksheet, LOG_HEADER, grid)

    rows = []
    for number, row in enumerate(grid, start=1):
        if number == 1 or not any(cell.strip() for cell in row):
            continue
        padded = list(row) + [""] * (len(LOG_HEADER) - len(row))
        rows.append((number, dict(zip(LOG_HEADER, padded))))
    return rows


def _is_reversed(value: str, row_number: int) -> bool:
    """Interpret the `reversed` cell, refusing anything ambiguous.

    Blank means live; an explicit "yes"/"true"/"1" (any case, stripped)
    means undone. Anything else -- a stray "no", a typo, a note -- is
    refused rather than treated as falsy. Failing open here would let a
    re-posted screenshot silently double-pay: a human typing "no" in that
    column would make image_already_logged treat the row as not-reversed
    and process the screenshot a second time.
    """
    normalised = value.strip().casefold()
    if not normalised:
        return False
    if normalised in REVERSED_TRUE_VALUES:
        return True
    raise SheetStructureError(
        f"Row {row_number} in worksheet {LOG_TAB!r} has reversed={value!r}, "
        "which is neither blank nor yes/true/1; refusing to guess whether "
        "it was undone"
    )


def parse_image_hashes(value: str) -> list[str]:
    """The hashes held in an `image_sha256` cell, in either stored format.

    One message can carry several screenshots, so new rows store a JSON
    list of hashes -- one per image. Rows written before that stored a
    single bare hex string, and the live sheet already contains them.
    Both must read correctly: treating a legacy bare string as malformed
    would make every existing row invisible to duplicate detection.

    The column NAME is unchanged, so there is no header migration and
    LOG_HEADER stays as it is -- only the value format is new.

    Unparseable content yields no hashes rather than raising. This is the
    one deliberate exception to this module's refuse-rather-than-guess
    rule, and it is narrow: duplicate detection is advisory (it warns an
    officer, it does not block them), so a corrupt cell degrades to "not
    flagged as a duplicate" -- the officer still sees the full preview and
    still decides. Raising instead would take down !attendance entirely
    for every future screenshot because of one bad row.
    """
    raw = (value or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(h).strip() for h in parsed if str(h).strip()]
    return [raw]


def any_image_already_logged(spreadsheet, image_hashes) -> bool:
    """True if ANY of these images was logged and not later reversed.

    Keyed on each image's own content hash, not attachment_id. Discord
    mints a new attachment_id on every upload, so the same PNG re-posted
    a week later gets a brand-new id and attachment_id-based detection
    would never notice -- it would look like a new, distinct screenshot
    every time. The hash is the same every time the same picture is
    posted, which is what "already logged" actually needs to mean.

    ANY-match rather than all-match, because the realistic double-pay is a
    PARTIAL re-post: someone posting just one of the two screenshots from
    an earlier rally. A single combined hash over all the images would
    differ from the original and sail straight through.
    """
    wanted = {h for h in (str(x).strip() for x in image_hashes) if h}
    if not wanted:
        return False

    for number, entry in _log_rows(spreadsheet):
        # Hash first, then the reversed check: an unrelated row with a
        # malformed `reversed` cell must not raise on a lookup that would
        # never have matched it anyway.
        if wanted & set(parse_image_hashes(entry["image_sha256"])):
            if not _is_reversed(entry["reversed"], number):
                return True
    return False


def image_already_logged(spreadsheet, image_sha256: str) -> bool:
    """True if this one screenshot was logged and not later reversed."""
    return any_image_already_logged(spreadsheet, [image_sha256])


def last_unreversed_entry(spreadsheet) -> tuple[int, dict] | None:
    """Most recent log entry that has not been undone."""
    for number, entry in reversed(_log_rows(spreadsheet)):
        if not _is_reversed(entry["reversed"], number):
            return number, entry
    return None


def mark_entry_reversed(
    spreadsheet, row_number: int, expected_attachment_id: str
) -> None:
    """Flag a log entry as undone.

    `expected_attachment_id` must be the attachment_id of the entry the
    caller read at `row_number` (from last_unreversed_entry). Before
    writing, this re-reads that row and refuses unless it still holds that
    same attachment_id and is still unreversed.

    Concurrency note: last_unreversed_entry (read) and mark_entry_reversed
    (write) are two separate round trips with no lock between them. Two
    officers running !undoattendance at once can both read the same row as
    "the one to undo"; without this check, both writes would land, points
    would come off twice, and the entry underneath would never get its
    turn. The re-check makes the second writer's call fail loudly instead
    of double-reversing. It does not by itself serialise the
    point-subtracting side of an undo (plan_point_writes/apply_writes) --
    the caller is still responsible for treating read-plan-write as one
    unit against other undo attempts, same as plan_point_writes/apply_writes.
    """
    worksheet = _worksheet(spreadsheet, LOG_TAB)
    grid = _grid(worksheet)
    if row_number - 1 >= len(grid):
        raise SheetStructureError(
            f"Row {row_number} no longer exists in worksheet {LOG_TAB!r}; "
            "refusing to mark reversed"
        )
    row = grid[row_number - 1]
    padded = list(row) + [""] * (len(LOG_HEADER) - len(row))
    current_attachment_id = padded[LOG_HEADER.index("attachment_id")]
    if current_attachment_id != expected_attachment_id:
        raise SheetStructureError(
            f"Row {row_number} in worksheet {LOG_TAB!r} now holds "
            f"attachment_id {current_attachment_id!r}, not "
            f"{expected_attachment_id!r}; refusing to mark the wrong entry "
            "reversed"
        )
    if _is_reversed(padded[LOG_HEADER.index("reversed")], row_number):
        raise SheetStructureError(
            f"Row {row_number} in worksheet {LOG_TAB!r} was already marked "
            "reversed; refusing to double-reverse it"
        )
    worksheet.update_cell(row_number, LOG_HEADER.index("reversed") + 1, "yes")
