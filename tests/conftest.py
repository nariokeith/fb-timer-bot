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

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def batch_update(self, data):
        self.batches.append(data)

    def append_row(self, values, **kwargs):
        self.appended.append(list(values))

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
