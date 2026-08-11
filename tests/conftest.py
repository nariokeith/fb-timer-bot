"""Fakes shared across the attendance test modules."""

from dataclasses import dataclass, field


@dataclass
class FakeInteraction:
    output_text: str


@dataclass
class _FakeInteractions:
    output_text: str = ""
    error: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FakeInteraction(output_text=self.output_text)


@dataclass
class FakeGeminiClient:
    """Stands in for google.genai.Client in tests."""

    output_text: str = ""
    error: Exception | None = None

    def __post_init__(self):
        self.interactions = _FakeInteractions(
            output_text=self.output_text, error=self.error
        )

    @property
    def calls(self):
        return self.interactions.calls


class FakeWorksheet:
    """Stands in for a gspread Worksheet.

    Holds the grid exactly as get_all_values() returns it: every cell a
    string, blanks as "".
    """

    def __init__(self, rows: list[list[str]], title: str = "Week 17"):
        self._rows = [list(r) for r in rows]
        self.title = title
        self.batches: list[list[dict]] = []
        self.appended: list[list] = []
        # Set by FakeSpreadsheet when this sheet is registered, so grid
        # reads count against the same tally as spreadsheet-level calls.
        # None for a worksheet built standalone, as many tests do.
        self.spreadsheet = None

    def get_all_values(self):
        if self.spreadsheet is not None:
            self.spreadsheet.reads += 1
        return [list(r) for r in self._rows]

    def batch_update(self, data):
        self.batches.append(data)

    def append_row(self, values, **kwargs):
        self.appended.append(list(values))
        self._rows.append(list(values))

    def update_cell(self, row, col, value):
        while len(self._rows) < row:
            self._rows.append([])
        target = self._rows[row - 1]
        while len(target) < col:
            target.append("")
        target[col - 1] = str(value)


SAMPLE_GRID = [
    ["Player Name", "Points", "Lucus - 3", "EGO", "Livera", "Lady Dalia"],
    ["ARCILynN", "51", "", "1", "3", "3"],
    ["xSigarilyas", "49", "3", "2", "3", "4"],
    ["Kobe", "44", "", "1", "3", "2"],
    ["wileKAMOTE卐", "36", "", "", "3", "3"],
]


import gspread
import gspread.utils


def _trimmed(rows: list[list[str]]) -> list[list[str]]:
    """The grid as the Sheets API returns it over the wire.

    values.batchGet omits trailing empty cells and trailing empty rows
    rather than padding to a rectangle. Reproducing that here is what
    makes the padding step in the caller (gspread's fill_gaps) real
    rather than a no-op the tests never exercise.
    """
    out = []
    for row in rows:
        cells = list(row)
        while cells and cells[-1] == "":
            cells.pop()
        out.append(cells)
    while out and not out[-1]:
        out.pop()
    return out


class FakeSpreadsheet:
    """Stands in for a gspread Spreadsheet holding FakeWorksheets.

    Counts API reads in `reads`. Every method here that the real gspread
    backs with an HTTP round trip increments it -- including worksheet(),
    which refetches the spreadsheet metadata on every single call
    (gspread/spreadsheet.py: `sheet_data = self.fetch_sheet_metadata()`).
    That hidden cost is the thing these tests exist to pin down.
    """

    def __init__(self, sheets: dict[str, FakeWorksheet] | None = None):
        self._sheets = dict(sheets or {})
        self.created: list[str] = []
        self.reads = 0
        for sheet in self._sheets.values():
            sheet.spreadsheet = self

    def worksheet(self, title):
        self.reads += 1
        try:
            return self._sheets[title]
        except KeyError:
            raise gspread.exceptions.WorksheetNotFound(title) from None

    def worksheets(self):
        self.reads += 1
        return list(self._sheets.values())

    def values_batch_get(self, ranges, params=None):
        self.reads += 1
        by_range = {
            gspread.utils.absolute_range_name(title): sheet
            for title, sheet in self._sheets.items()
        }
        value_ranges = []
        for range_name in ranges:
            if range_name not in by_range:
                # The real API answers a range naming a missing tab with
                # 400, not an empty result. Surfacing it as a test-time
                # error keeps a caller from "passing" on a range that
                # would fail in production.
                raise ValueError(f"no such range in this spreadsheet: {range_name!r}")
            values = _trimmed(by_range[range_name]._rows)
            # The API echoes the range it actually resolved, with the A1
            # span appended -- not the bare range that was requested.
            rows = len(values) or 1
            cols = max((len(row) for row in values), default=1) or 1
            span = "A1:{}".format(gspread.utils.rowcol_to_a1(rows, cols))
            entry = {"range": "{}!{}".format(range_name, span)}
            if values:
                entry["values"] = values
            value_ranges.append(entry)
        return {"valueRanges": value_ranges}

    def add_worksheet(self, title, rows=100, cols=20):
        ws = FakeWorksheet([], title=title)
        ws.spreadsheet = self
        self._sheets[title] = ws
        self.created.append(title)
        return ws
