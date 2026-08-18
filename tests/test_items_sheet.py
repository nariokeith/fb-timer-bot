"""Tests for Logs Tracker access, against the shared gspread fakes."""

import gspread
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


def test_snapshot_refuses_a_reordered_ledger_header():
    spreadsheet = make_spreadsheet()
    spreadsheet.worksheet(items_sheet.LEDGER_TAB)._rows[0][0:2] = ["IGN", "Timestamp (PHT)"]
    with pytest.raises(SheetStructureError) as exc:
        items_sheet.read_snapshot(spreadsheet)
    assert "Distribution Log" in str(exc.value)


def test_snapshot_costs_two_api_reads():
    """The whole point of the snapshot: one command, minimal quota.

    Sheets allows 60 reads per minute per credential, and both bots share
    one service account. Resolving each tab with spreadsheet.worksheet()
    cost a hidden metadata fetch apiece on top of the values call, so one
    !request burned seven reads and a busy minute returned 429 to
    everyone. Metadata once, values once.
    """
    spreadsheet = make_spreadsheet()
    items_sheet.read_snapshot(spreadsheet)
    assert spreadsheet.reads == 2


def test_snapshot_pads_ragged_rows_from_the_api():
    """batchGet omits trailing blanks; get_all_values padded them back.

    This pins the switch to batchGet as behaviour-preserving. The grids
    reach the same consumers as before, so they must arrive in the same
    shape -- rectangular -- rather than ragged. Today's consumers happen
    to bounds-check (holds_special guards with len(row) < column), so
    this is not a live crash; it is the guarantee that lets the next
    consumer index special_grid the way get_all_values always allowed.
    """
    grid = [
        ["Player Name", "Asta's Heart", "Amentis' Foot"],
        ["Kobe", "TRUE", "FALSE"],
        ["Dajz", "", ""],
    ]
    spreadsheet = FakeSpreadsheet(
        {items_sheet.SPECIAL_TAB: FakeWorksheet(grid, title=items_sheet.SPECIAL_TAB)}
    )
    snapshot = items_sheet.read_snapshot(spreadsheet)
    assert all(len(row) == 3 for row in snapshot.special_grid)
    assert items_sheet.holds_special(snapshot, "Dajz", "Amentis' Foot") is False


def rate_limited(message="Quota exceeded for quota metric 'Read requests'"):
    """A gspread APIError shaped exactly like a real Sheets 429.

    Built through APIError's own constructor rather than a stub, so the
    .code the retry logic branches on is the one gspread would really
    populate.
    """

    class _Response:
        status_code = 429
        text = message

        def json(self):
            return {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}}

    return gspread.exceptions.APIError(_Response())


class FlakySpreadsheet:
    """Fails the first `failures` snapshot reads with 429, then works."""

    def __init__(self, inner, failures: int, error=None):
        self._inner = inner
        self._remaining = failures
        self._error = error or rate_limited()
        self.attempts = 0

    def worksheets(self):
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return self._inner.worksheets()

    def values_batch_get(self, ranges, params=None):
        return self._inner.values_batch_get(ranges, params)


def test_snapshot_retries_a_rate_limited_read_and_succeeds():
    """A 429 is a "come back shortly", not a failed request.

    Two bots share one 60-per-minute credential, so a burst can refuse a
    read that would succeed seconds later. Retrying keeps that off the
    member's screen entirely.
    """
    slept = []
    spreadsheet = FlakySpreadsheet(make_spreadsheet(), failures=2)
    snapshot = items_sheet.read_snapshot(spreadsheet, sleep=slept.append)
    assert "Kobe" in snapshot.roster
    assert spreadsheet.attempts == 3
    assert slept == list(items_sheet.RETRY_DELAYS)


def test_snapshot_gives_up_after_the_last_retry():
    spreadsheet = FlakySpreadsheet(make_spreadsheet(), failures=99)
    with pytest.raises(gspread.exceptions.APIError) as exc:
        items_sheet.read_snapshot(spreadsheet, sleep=lambda _: None)
    assert exc.value.code == 429
    assert spreadsheet.attempts == len(items_sheet.RETRY_DELAYS) + 1


def test_snapshot_does_not_retry_a_non_quota_error():
    """Only 429 is worth waiting on.

    A revoked key or a deleted sheet fails identically on every attempt,
    so retrying would just make the member wait seconds for the same
    refusal.
    """
    denied = rate_limited("caller does not have permission")
    denied.code = 403
    spreadsheet = FlakySpreadsheet(make_spreadsheet(), failures=99, error=denied)
    with pytest.raises(gspread.exceptions.APIError):
        items_sheet.read_snapshot(spreadsheet, sleep=lambda _: None)
    assert spreadsheet.attempts == 1


def test_snapshot_matches_grids_by_range_not_by_position():
    """Each grid is claimed by name, so response order cannot mix them up.

    Pairing the response with the request list positionally would put the
    Gear Logs grid into special_grid the moment the API answered in a
    different order -- silently, and with every checkbox answer wrong
    from then on. Nothing in this bot should rest on that ordering.
    """

    class Reordering:
        def __init__(self, inner):
            self._inner = inner

        def worksheets(self):
            return self._inner.worksheets()

        def values_batch_get(self, ranges, params=None):
            response = self._inner.values_batch_get(ranges, params)
            return {"valueRanges": list(reversed(response["valueRanges"]))}

    snapshot = items_sheet.read_snapshot(Reordering(make_spreadsheet()))
    assert "Asta's Heart" in snapshot.special_headers
    assert "Asta's Belt" in snapshot.gear_headers
    assert snapshot.ledger_rows[0][1] == "Kobe"


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


def test_find_row_refuses_duplicate_normalized_players():
    worksheet = FakeWorksheet([SPECIAL_GRID[0], ["Kobe", "FALSE"], [" kobe ", "FALSE"]])
    with pytest.raises(SheetStructureError) as exc:
        items_sheet.find_row(worksheet, "Kobe")
    assert "2, 3" in str(exc.value)


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


def test_record_special_refuses_an_unknown_checkbox_value():
    grid = [row[:] for row in SPECIAL_GRID]
    grid[2][1] = "officer note"
    spreadsheet = FakeSpreadsheet({items_sheet.SPECIAL_TAB: FakeWorksheet(grid, title=items_sheet.SPECIAL_TAB)})
    with pytest.raises(SheetStructureError) as exc:
        items_sheet.record_special(spreadsheet, "Dajz", "Asta's Heart")
    assert "officer note" in str(exc.value)
    assert spreadsheet.worksheet(items_sheet.SPECIAL_TAB).batches == []


def test_already_recorded_finds_request_ids_in_the_ledger():
    snapshot = items_sheet.read_snapshot(make_spreadsheet())
    assert items_sheet.already_recorded(snapshot, "aaa")
    assert not items_sheet.already_recorded(snapshot, "missing")


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


def _api_error_with_code(code, message):
    error = rate_limited(message)
    error.code = code
    return error


@pytest.mark.parametrize(
    "code,message",
    [
        (503, "The service is currently unavailable."),
        (500, "Internal error encountered."),
        (502, "Bad gateway."),
        (504, "Deadline exceeded."),
    ],
)
def test_snapshot_retries_a_transient_server_error(code, message):
    """Google's 5xx family is "try again", exactly like a 429.

    A 503 is Sheets' standard transient backend refusal -- Google's own
    guidance is to retry it with backoff. Treating it like a revoked key
    put "APIError: [503]: The service is currently unavailable" in front
    of a member trying to run !request, for a read that would have
    succeeded a second later.
    """
    slept = []
    spreadsheet = FlakySpreadsheet(
        make_spreadsheet(), failures=2, error=_api_error_with_code(code, message)
    )

    snapshot = items_sheet.read_snapshot(spreadsheet, sleep=slept.append)

    assert "Kobe" in snapshot.roster
    assert spreadsheet.attempts == 3
    assert slept == list(items_sheet.RETRY_DELAYS)


@pytest.mark.parametrize("code", [403, 404, 400])
def test_snapshot_still_refuses_a_permanent_error_immediately(code):
    """The original reasoning holds for these: same answer every time."""
    spreadsheet = FlakySpreadsheet(
        make_spreadsheet(), failures=99, error=_api_error_with_code(code, "nope")
    )

    with pytest.raises(gspread.exceptions.APIError):
        items_sheet.read_snapshot(spreadsheet, sleep=lambda _: None)

    assert spreadsheet.attempts == 1, "a permanent failure must not cost a wait"
