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
