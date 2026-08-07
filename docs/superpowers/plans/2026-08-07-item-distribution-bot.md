# Item Distribution Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A third Discord bot that queues member item requests, lets officers approve or deny them from one panel, enforces the guild's allocation caps, and writes every approval to the Logs Tracker spreadsheet.

**Architecture:** Four new modules with one responsibility each — `items_rules.py` (pure logic, no I/O), `items_state.py` (pinned-message persistence, pure), `items_sheet.py` (Google Sheets access), `items_bot.py` (Discord wiring). The bot runs as a third OS process under the existing `supervisor.py`, with its own Discord token and its own spreadsheet, so it cannot affect the two production bots.

**Tech Stack:** Python 3.13, `discord.py` 2.7.1, `gspread` 6.2.1, `google-auth`, pytest. No new dependencies.

## Global Constraints

- **Never modify `bot.py` or `attendance_bot.py`.** Changes to `supervisor.py` and `render.yaml` are additive only.
- **Never create a player row or an item column.** Unknown input is refused, following the refuse-rather-than-guess convention in `attendance_roster.py` and `attendance_sheet.find_column`.
- Timezone is `Asia/Manila` throughout. Ledger timestamps are stored in PHT local time as `%Y-%m-%d %H:%M:%S`.
- Spreadsheet ID: `1Xx44UKBx0v5Pa0xbBzuVElEFZK-mdeQ5jHBBzBsKQgc` (env var `ITEMS_SHEET_ID`). This is **not** the attendance `SHEET_ID`.
- Tab names, verbatim: `Special Logs`, `Gear Logs`, `Distribution Log`.
- Gear daily cap default 3, overridable via `ITEMS_GEAR_DAILY_CAP`.
- Missing credentials at startup → print to stderr, `sys.exit(78)`.
- All blocking `gspread` calls go through `asyncio.to_thread`; every read-then-write sequence is serialized by a module-level `asyncio.Lock`.
- The `Gear Logs` tab is still being edited by the user. Never cache or hardcode an item list — read headers live on every request.
- Reuse, do not reimplement: `attendance_roster.normalize` / `match_names`, `attendance_sheet.open_spreadsheet` / `read_headers` / `read_players` / `find_column` / `get_or_create_tab` / `SheetStructureError`, `attendance_bosses.header_base`.
- Run tests with `.venv/bin/python -m pytest` (NOT `.venv/bin/pytest` — that does not put the repo root on sys.path, so every `import items_*` fails at collection). Commit after every task.

---

## File Structure

| File | Responsibility | Pure? |
|---|---|---|
| `items_rules.py` | PHT day boundary, cap arithmetic, item-type resolution, `!request` parsing | yes |
| `items_state.py` | Encode/decode the pinned state message, queue operations | yes |
| `items_sheet.py` | Logs Tracker reads and writes | no (gspread) |
| `items_bot.py` | Discord commands, panel embed, button/select handlers | no (discord.py) |
| `tests/test_items_rules.py` | | |
| `tests/test_items_state.py` | | |
| `tests/test_items_sheet.py` | | |
| `tests/test_items_bot.py` | | |
| `supervisor.py` | **modify**: add a third `ChildSpec` | |
| `render.yaml` | **modify**: add two env vars | |
| `README.md` | **modify**: document the bot | |

---

## Task 1: Day boundary and gear cap arithmetic

**Files:**
- Create: `items_rules.py`
- Test: `tests/test_items_rules.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PHT`, `TIMESTAMP_FORMAT`, `now_pht() -> datetime`, `format_timestamp(dt) -> str`, `pht_day(timestamp: str) -> str`, `gear_used_today(ledger_rows, ign, today, *, ign_column=1, type_column=3, timestamp_column=0) -> int`, `DEFAULT_GEAR_DAILY_CAP = 3`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_items_rules.py
"""Tests for the pure allocation logic."""

from datetime import datetime

import items_rules


def test_pht_day_is_the_date_prefix_of_a_timestamp():
    assert items_rules.pht_day("2026-08-07 23:59:59") == "2026-08-07"


def test_one_minute_past_midnight_is_a_different_day():
    before = items_rules.pht_day("2026-08-07 23:59:00")
    after = items_rules.pht_day("2026-08-08 00:01:00")
    assert before != after


def test_format_timestamp_round_trips_through_pht_day():
    moment = datetime(2026, 8, 7, 23, 59, 0, tzinfo=items_rules.PHT)
    assert items_rules.pht_day(items_rules.format_timestamp(moment)) == "2026-08-07"


def test_now_pht_is_timezone_aware_in_manila():
    assert items_rules.now_pht().tzinfo is items_rules.PHT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'items_rules'`

- [ ] **Step 3: Write minimal implementation**

```python
# items_rules.py
"""Allocation rules for the item distribution bot.

Pure logic only -- no Google Sheets, no Discord, no clock reads except
now_pht(). Everything here is decided from values the caller passes in,
which is what makes the caps testable without a network.

The guild's two rules:
  * a special log may be received once, ever
  * gear logs are capped at three per player per PHT day, any mix

The second rule cannot be answered from the Gear Logs tab: its cells hold
lifetime totals with no dates. It is answered from the Distribution Log
ledger instead, which is why gear_used_today takes ledger rows.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from attendance_roster import ALIASES, normalize

PHT = ZoneInfo("Asia/Manila")

# Ledger timestamps are PHT local time with no offset suffix. That makes
# "which day is this row on" a string prefix comparison rather than a
# parse-and-convert, so a malformed row can never be silently counted
# into the wrong day.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_GEAR_DAILY_CAP = 3

SPECIAL = "Special"
GEAR = "Gear"


def now_pht() -> datetime:
    return datetime.now(PHT)


def format_timestamp(moment: datetime) -> str:
    return moment.strftime(TIMESTAMP_FORMAT)


def pht_day(timestamp: str) -> str:
    """The date portion of a ledger timestamp, as 'YYYY-MM-DD'."""
    return timestamp.strip()[:10]


def gear_used_today(
    ledger_rows: list[list[str]],
    ign: str,
    today: str,
    *,
    timestamp_column: int = 0,
    ign_column: int = 1,
    type_column: int = 3,
) -> int:
    """How many gear logs this player has already been given today.

    Rows too short to hold the columns we need are skipped rather than
    raising: a half-written ledger row must not make the cap
    uncomputable and lock the player out entirely.
    """
    wanted = normalize(ign)
    count = 0
    for row in ledger_rows:
        if len(row) <= max(timestamp_column, ign_column, type_column):
            continue
        if row[type_column].strip() != GEAR:
            continue
        if normalize(row[ign_column]) != wanted:
            continue
        if pht_day(row[timestamp_column]) == today:
            count += 1
    return count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Add the cap-counting tests**

```python
# append to tests/test_items_rules.py

LEDGER = [
    ["2026-08-07 09:00:00", "Kobe", "Asta's Belt", "Gear", "Officer", "1", "aaa"],
    ["2026-08-07 10:00:00", "Kobe", "Benji's Heart", "Gear", "Officer", "1", "bbb"],
    ["2026-08-07 11:00:00", "Dajz", "Benji's Heart", "Gear", "Officer", "2", "ccc"],
    ["2026-08-06 23:59:00", "Kobe", "Amentis' Foot", "Gear", "Officer", "1", "ddd"],
    ["2026-08-07 12:00:00", "Kobe", "Asta's Heart", "Special", "Officer", "1", "eee"],
]


def test_counts_only_this_players_gear_rows_from_today():
    assert items_rules.gear_used_today(LEDGER, "Kobe", "2026-08-07") == 2


def test_special_rows_never_count_against_the_gear_cap():
    only_special = [r for r in LEDGER if r[3] == "Special"]
    assert items_rules.gear_used_today(only_special, "Kobe", "2026-08-07") == 0


def test_yesterdays_rows_do_not_count():
    assert items_rules.gear_used_today(LEDGER, "Kobe", "2026-08-06") == 1


def test_ign_matching_ignores_case_and_spacing():
    assert items_rules.gear_used_today(LEDGER, "  kobe ", "2026-08-07") == 2


def test_short_rows_are_skipped_not_fatal():
    assert items_rules.gear_used_today([["2026-08-07 09:00:00"]], "Kobe", "2026-08-07") == 0
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 7: Commit**

```bash
git add items_rules.py tests/test_items_rules.py
git commit -m "Add the PHT day boundary and gear cap arithmetic"
```

---

## Task 2: Item-type resolution

**Files:**
- Modify: `items_rules.py`
- Test: `tests/test_items_rules.py`

**Interfaces:**
- Consumes: `items_rules.SPECIAL`, `items_rules.GEAR` from Task 1.
- Produces: `ItemLookupError`, `ResolvedItem` (frozen dataclass: `name: str`, `type: str`), `resolve_item(query, special_headers, gear_headers) -> ResolvedItem`.

An item's type is derived from which tab holds its column — never configured. Found in both tabs is an error, not a preference, matching `attendance_sheet.find_column`'s refusal to pick between duplicate columns.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_items_rules.py
import pytest

SPECIAL_HEADERS = ["Player Name", "Asta's Heart", "Amentis' Foot", "Benji's Blood"]
GEAR_HEADERS = ["Player Name", "Asta's Belt", "Benji's Heart"]


def test_resolves_an_item_to_the_tab_that_holds_it():
    found = items_rules.resolve_item("Asta's Heart", SPECIAL_HEADERS, GEAR_HEADERS)
    assert (found.name, found.type) == ("Asta's Heart", items_rules.SPECIAL)


def test_resolves_a_gear_item():
    found = items_rules.resolve_item("Asta's Belt", SPECIAL_HEADERS, GEAR_HEADERS)
    assert (found.name, found.type) == ("Asta's Belt", items_rules.GEAR)


def test_matching_ignores_case():
    found = items_rules.resolve_item("asta's belt", SPECIAL_HEADERS, GEAR_HEADERS)
    assert found.name == "Asta's Belt"


def test_a_partial_item_name_is_refused_but_lists_the_matches():
    """A member who types 'Asta' should be shown the Asta items.

    difflib scores 'Asta' against "Asta's Heart" at 0.5, so this only
    works because _suggest searches substrings before close matches.
    """
    with pytest.raises(items_rules.ItemLookupError) as exc:
        items_rules.resolve_item("Asta", SPECIAL_HEADERS, GEAR_HEADERS)
    assert "Asta's Heart" in str(exc.value)


def test_a_typo_is_refused_but_offers_the_close_match():
    """No substring overlap here -- this is the close-match path."""
    with pytest.raises(items_rules.ItemLookupError) as exc:
        items_rules.resolve_item("Astas Hesrt", SPECIAL_HEADERS, GEAR_HEADERS)
    assert "Asta's Heart" in str(exc.value)


def test_an_item_resembling_nothing_gets_no_suggestions():
    with pytest.raises(items_rules.ItemLookupError) as exc:
        items_rules.resolve_item("zzzzzzzz", SPECIAL_HEADERS, GEAR_HEADERS)
    assert "Did you mean" not in str(exc.value)


def test_an_item_in_both_tabs_is_refused_rather_than_guessed():
    with pytest.raises(items_rules.ItemLookupError) as exc:
        items_rules.resolve_item("Shared", ["Player Name", "Shared"], ["Player Name", "Shared"])
    assert "both" in str(exc.value).lower()


def test_the_player_name_column_is_never_treated_as_an_item():
    with pytest.raises(items_rules.ItemLookupError):
        items_rules.resolve_item("Player Name", SPECIAL_HEADERS, GEAR_HEADERS)


def test_a_blank_query_is_refused():
    with pytest.raises(items_rules.ItemLookupError):
        items_rules.resolve_item("   ", SPECIAL_HEADERS, GEAR_HEADERS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -k resolve -v`
Expected: FAIL with `AttributeError: module 'items_rules' has no attribute 'resolve_item'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to items_rules.py
from dataclasses import dataclass
from difflib import get_close_matches

from attendance_bosses import header_base

PLAYER_COLUMN_HEADER = "Player Name"


class ItemLookupError(RuntimeError):
    """The item name does not resolve to exactly one column."""


@dataclass(frozen=True)
class ResolvedItem:
    name: str
    type: str


def item_names(headers: list[str]) -> list[str]:
    """Header cells that name an item.

    header_base strips a ' - N' annotation, matching the attendance
    sheet's header convention. Blanks (spacer columns) and the player
    column are not items.
    """
    names = []
    for cell in headers:
        base = header_base(cell)
        if not base or base == PLAYER_COLUMN_HEADER:
            continue
        names.append(base)
    return names


def _suggest(query: str, names: list[str]) -> list[str]:
    """Item names worth offering after a failed lookup.

    Substring hits come first and carry the weight: a member who types
    'Asta' wants to see every Asta item, but difflib scores 'Asta'
    against "Asta's Heart" at 0.5 -- below any cutoff loose enough to be
    safe -- so close-matching alone would offer nothing at all. Close
    matches still follow, to catch the transposed-letter typo that a
    substring search cannot.
    """
    wanted = normalize(query)
    hits = [name for name in names if wanted and wanted in normalize(name)]
    for name in get_close_matches(query, names, n=3, cutoff=0.6):
        if name not in hits:
            hits.append(name)
    return hits[:3]


def _exact(query: str, names: list[str]) -> str | None:
    wanted = normalize(query)
    for name in names:
        if normalize(name) == wanted:
            return name
    return None


def resolve_item(
    query: str, special_headers: list[str], gear_headers: list[str]
) -> ResolvedItem:
    """Which item this is, and which tab it lives in.

    Requires an exact (case- and spacing-insensitive) header match. Fuzzy
    matching is deliberately NOT used here: item names differ by one word
    ("Asta's Belt" vs "Asta's Heart"), so a near match would hand out the
    wrong item -- and unlike attendance, an approval is a permanent
    record. Close names are offered as suggestions instead.
    """
    if not query.strip():
        raise ItemLookupError("No item name given.")

    specials = item_names(special_headers)
    gears = item_names(gear_headers)

    in_special = _exact(query, specials)
    in_gear = _exact(query, gears)

    if in_special and in_gear:
        raise ItemLookupError(
            f"{in_special!r} appears in both Special Logs and Gear Logs; "
            "refusing to guess which one is meant. Remove the duplicate column."
        )
    if in_special:
        return ResolvedItem(name=in_special, type=SPECIAL)
    if in_gear:
        return ResolvedItem(name=in_gear, type=GEAR)

    suggestions = _suggest(query, specials + gears)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ItemLookupError(f"No item column named {query!r}.{hint}")
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add items_rules.py tests/test_items_rules.py
git commit -m "Derive an item's type from the tab whose header holds it"
```

---

## Task 3: `!request` parsing by matching

**Files:**
- Modify: `items_rules.py`
- Test: `tests/test_items_rules.py`

**Interfaces:**
- Consumes: `resolve_item`, `ItemLookupError`, `ResolvedItem` from Task 2.
- Produces: `RequestParseError`, `ParsedRequest` (frozen dataclass: `item: ResolvedItem`, `ign: str`), `parse_request(argument, roster, special_headers, gear_headers) -> ParsedRequest`.

Both the item name and the IGN contain spaces (the roster holds `chinchong ni Mumu`), so no fixed split point works. Try every split; accept the one where the suffix is a known IGN and the prefix a known item.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_items_rules.py

ROSTER = ["Kobe", "Dajz", "chinchong ni Mumu", "Smth"]


def _parse(argument):
    return items_rules.parse_request(argument, ROSTER, SPECIAL_HEADERS, GEAR_HEADERS)


def test_parses_a_single_word_ign():
    parsed = _parse("Asta's Heart Kobe")
    assert (parsed.item.name, parsed.ign) == ("Asta's Heart", "Kobe")


def test_parses_an_ign_containing_spaces():
    parsed = _parse("Asta's Heart chinchong ni Mumu")
    assert (parsed.item.name, parsed.ign) == ("Asta's Heart", "chinchong ni Mumu")


def test_parses_a_gear_item():
    parsed = _parse("Asta's Belt Dajz")
    assert (parsed.item.name, parsed.item.type) == ("Asta's Belt", items_rules.GEAR)


def test_extra_whitespace_does_not_matter():
    parsed = _parse("  Asta's   Heart    Kobe ")
    assert parsed.ign == "Kobe"


def test_an_unknown_ign_is_refused_and_named():
    with pytest.raises(items_rules.RequestParseError) as exc:
        _parse("Asta's Heart Kobee")
    assert "Kobee" in str(exc.value) or "Kobe" in str(exc.value)


def test_an_unknown_item_is_refused():
    with pytest.raises(items_rules.RequestParseError):
        _parse("Nonexistent Thing Kobe")


def test_a_bare_ign_with_no_item_is_refused():
    with pytest.raises(items_rules.RequestParseError):
        _parse("Kobe")


def test_an_empty_argument_is_refused():
    with pytest.raises(items_rules.RequestParseError):
        _parse("")


def test_an_alias_resolves_to_the_sheet_row():
    """'ツRyuuツ' shares no Latin characters with 'Ryuu'.

    Aliases are the only possible resolution path for names like this --
    no fuzzy threshold, however low, could ever match them.
    """
    parsed = items_rules.parse_request(
        "Asta's Heart ツRyuuツ", ["Ryuu"], SPECIAL_HEADERS, GEAR_HEADERS
    )
    assert parsed.ign == "Ryuu"


def test_an_alias_whose_target_is_absent_does_not_resolve():
    with pytest.raises(items_rules.RequestParseError):
        items_rules.parse_request(
            "Asta's Heart ツRyuuツ", ["Kobe"], SPECIAL_HEADERS, GEAR_HEADERS
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -k parse -v`
Expected: FAIL with `AttributeError: module 'items_rules' has no attribute 'parse_request'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to items_rules.py


class RequestParseError(RuntimeError):
    """The !request argument does not resolve to one item and one player."""


@dataclass(frozen=True)
class ParsedRequest:
    item: ResolvedItem
    ign: str


def resolve_ign(query: str, roster: list[str]) -> str | None:
    """The roster entry this name refers to, or None.

    Exact (normalized) match, plus attendance_roster.ALIASES so a member
    typing their decorated in-game name ('ツRyuuツ') reaches their sheet
    row ('Ryuu'). Some in-game names share no characters at all with
    their sheet row, so aliases are the only possible resolution path
    for those players -- no fuzzy threshold could ever match them.

    Fuzzy matching is deliberately not used: a wrong match here would
    credit an item to someone else, permanently.
    """
    index = {normalize(player): player for player in roster if player.strip()}
    wanted = normalize(query)
    if wanted in index:
        return index[wanted]
    for alias, target in ALIASES.items():
        if normalize(alias) == wanted and normalize(target) in index:
            return index[normalize(target)]
    return None


def parse_request(
    argument: str,
    roster: list[str],
    special_headers: list[str],
    gear_headers: list[str],
) -> ParsedRequest:
    """Split '<item name> <IGN>' where BOTH parts may contain spaces.

    Position-based splitting cannot work: the roster contains
    'chinchong ni Mumu'. Every split point is tried instead, and a split
    is accepted only when its suffix is a known player AND its prefix is
    a known item. Longest IGN first, so 'chinchong ni Mumu' wins over a
    hypothetical player called 'Mumu'.
    """
    words = argument.split()
    if len(words) < 2:
        raise RequestParseError(
            "Usage: `!request <item name> <IGN>` -- for example "
            "`!request Asta's Heart Kobe`"
        )

    matches: list[ParsedRequest] = []
    item_errors: list[str] = []
    saw_known_ign = False

    # split at index i => words[:i] is the item, words[i:] is the IGN.
    # Descending i would try the SHORTEST ign first; ascending tries the
    # longest, which is what disambiguates multi-word names.
    for i in range(1, len(words)):
        candidate_ign = " ".join(words[i:])
        player = resolve_ign(candidate_ign, roster)
        if player is None:
            continue
        saw_known_ign = True
        try:
            item = resolve_item(
                " ".join(words[:i]), special_headers, gear_headers
            )
        except ItemLookupError as exc:
            item_errors.append(str(exc))
            continue
        matches.append(ParsedRequest(item=item, ign=player))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        readings = "; ".join(f"{m.item.name!r} for {m.ign!r}" for m in matches)
        raise RequestParseError(
            f"That could be read more than one way ({readings}). "
            "Refusing to guess."
        )
    if saw_known_ign:
        raise RequestParseError(item_errors[0])

    tail = words[-1]
    suggestions = get_close_matches(tail, roster, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise RequestParseError(
        f"No player named {tail!r} in the sheet.{hint} "
        "The IGN goes last: `!request <item name> <IGN>`"
    )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add items_rules.py tests/test_items_rules.py
git commit -m "Parse !request by matching both parts, not by position"
```

---

## Task 4: Eligibility decision

**Files:**
- Modify: `items_rules.py`
- Test: `tests/test_items_rules.py`

**Interfaces:**
- Consumes: `SPECIAL`, `GEAR`, `gear_used_today`, `DEFAULT_GEAR_DAILY_CAP`.
- Produces: `Eligibility` (frozen dataclass: `allowed: bool`, `reason: str`, `used: int`, `cap: int`), `check_eligibility(item_type, ign, ledger_rows, today, *, already_has_special, pending_gear=0, cap=DEFAULT_GEAR_DAILY_CAP) -> Eligibility`.

This single function is used in **both** places the cap is enforced — at `!request` and again inside the lock when an officer approves. One implementation means the two checks can never drift apart.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_items_rules.py


def test_a_special_the_player_lacks_is_allowed():
    result = items_rules.check_eligibility(
        items_rules.SPECIAL, "Kobe", [], "2026-08-07", already_has_special=False
    )
    assert result.allowed


def test_a_special_the_player_already_has_is_refused():
    result = items_rules.check_eligibility(
        items_rules.SPECIAL, "Kobe", [], "2026-08-07", already_has_special=True
    )
    assert not result.allowed
    assert "already" in result.reason.lower()


def test_gear_under_the_cap_is_allowed():
    result = items_rules.check_eligibility(
        items_rules.GEAR, "Kobe", LEDGER, "2026-08-07", already_has_special=False
    )
    assert result.allowed
    assert (result.used, result.cap) == (2, 3)


def test_gear_at_the_cap_is_refused():
    ledger = LEDGER + [
        ["2026-08-07 13:00:00", "Kobe", "Asta's Belt", "Gear", "O", "1", "fff"]
    ]
    result = items_rules.check_eligibility(
        items_rules.GEAR, "Kobe", ledger, "2026-08-07", already_has_special=False
    )
    assert not result.allowed
    assert "3/3" in result.reason


def test_pending_requests_count_toward_the_cap():
    result = items_rules.check_eligibility(
        items_rules.GEAR,
        "Kobe",
        LEDGER,
        "2026-08-07",
        already_has_special=False,
        pending_gear=1,
    )
    assert not result.allowed


def test_the_cap_is_configurable():
    result = items_rules.check_eligibility(
        items_rules.GEAR, "Kobe", LEDGER, "2026-08-07", already_has_special=False, cap=2
    )
    assert not result.allowed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -q`
Expected: FAIL with `AttributeError: module 'items_rules' has no attribute 'check_eligibility'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to items_rules.py


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reason: str
    used: int = 0
    cap: int = 0


def check_eligibility(
    item_type: str,
    ign: str,
    ledger_rows: list[list[str]],
    today: str,
    *,
    already_has_special: bool,
    pending_gear: int = 0,
    cap: int = DEFAULT_GEAR_DAILY_CAP,
) -> Eligibility:
    """Whether this player may receive this item right now.

    Called twice per approval: once at !request for fast feedback, and
    again inside the write lock when an officer clicks approve. The
    second call is not redundant -- without it, a member queues several
    requests before any is approved and every one of them passes the
    first check.

    pending_gear is why the first call is meaningful at all: queued but
    unapproved gear requests count, so a member cannot stack four
    requests and have officers approve them one at a time, each
    individually looking within the cap.
    """
    if item_type == SPECIAL:
        if already_has_special:
            return Eligibility(
                allowed=False,
                reason="already has this special log -- it can only be received once",
            )
        return Eligibility(allowed=True, reason="eligible")

    used = gear_used_today(ledger_rows, ign, today) + pending_gear
    if used >= cap:
        return Eligibility(
            allowed=False,
            reason=f"already at the daily gear limit ({used}/{cap}) -- resets at midnight PHT",
            used=used,
            cap=cap,
        )
    return Eligibility(allowed=True, reason="eligible", used=used, cap=cap)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add items_rules.py tests/test_items_rules.py
git commit -m "Decide eligibility in one place used by both cap checks"
```

---

## Task 5: Pinned-message state

**Files:**
- Create: `items_state.py`
- Test: `tests/test_items_state.py`

**Interfaces:**
- Consumes: `items_rules.GEAR`, `items_rules.now_pht`, `items_rules.format_timestamp`.
- Produces: `STATE_MARKER`, `MAX_CONTENT`, `PendingRequest` (frozen dataclass: `id, user_id, ign, item, type, requested_at, note=""`), `State` (dataclass: `officer_channel_id: int | None`, `queue: list[PendingRequest]`, `igns: dict[str, str]`), `new_request_id() -> str`, `encode_state(state) -> tuple[str, list[PendingRequest]]`, `decode_state(content) -> State | None`, `pending_gear_for(state, ign) -> int`, `find_request(state, request_id) -> PendingRequest | None`, `remove_request(state, request_id) -> PendingRequest | None`.

Mirrors `bot.py`'s `FBTIMER_STATE_V1` mechanism, which is proven on this Render instance.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_items_state.py
"""Tests for the pinned-message state format and queue operations."""

import items_state


def _request(request_id="aaa", ign="Kobe", item="Asta's Heart", type_="Special"):
    return items_state.PendingRequest(
        id=request_id,
        user_id=42,
        ign=ign,
        item=item,
        type=type_,
        requested_at="2026-08-07 09:00:00",
    )


def test_encode_decode_round_trip():
    state = items_state.State(
        officer_channel_id=99, queue=[_request()], igns={"42": "Kobe"}
    )
    content, dropped = items_state.encode_state(state)
    assert dropped == []

    restored = items_state.decode_state(content)
    assert restored.officer_channel_id == 99
    assert restored.igns == {"42": "Kobe"}
    assert restored.queue[0].ign == "Kobe"
    assert restored.queue[0].id == "aaa"


def test_encoded_content_carries_the_marker():
    content, _ = items_state.encode_state(items_state.State())
    assert content.startswith(items_state.STATE_MARKER)


def test_decoding_an_unrelated_message_returns_none():
    assert items_state.decode_state("just a normal chat message") is None


def test_decoding_a_corrupt_payload_returns_none():
    assert items_state.decode_state(f"{items_state.STATE_MARKER}\n```json\n{{oops\n```") is None


def test_a_fresh_state_has_no_officer_channel():
    assert items_state.State().officer_channel_id is None


def test_oversize_state_drops_the_oldest_requests_and_reports_them():
    many = [_request(request_id=f"id{n:03d}", item="A Very Long Item Name Indeed") for n in range(300)]
    state = items_state.State(officer_channel_id=99, queue=many)

    content, dropped = items_state.encode_state(state)

    assert len(content) <= items_state.MAX_CONTENT
    assert dropped, "oversize state must report what it dropped"
    assert dropped[0].id == "id000", "the OLDEST request is dropped first"
    assert items_state.decode_state(content).officer_channel_id == 99


def test_new_request_ids_are_unique():
    assert len({items_state.new_request_id() for _ in range(200)}) == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'items_state'`

- [ ] **Step 3: Write minimal implementation**

```python
# items_state.py
"""The bot's state, stored as JSON in a pinned Discord message.

Render's free tier restarts on every deploy and can spin down, so an
in-memory queue silently loses pending requests. A tab in the
spreadsheet would work but turns every !request into a Sheets write on
the member-facing path. A pinned message costs nothing extra and is the
same mechanism bot.py already uses in production on this instance
(FBTIMER_STATE_V1).

Pure module: it produces and consumes strings. Reading and writing the
Discord message is items_bot's job.
"""

import json
import secrets
from dataclasses import dataclass, field

STATE_MARKER = "ITEMS_STATE_V1"

# Discord's hard limit is 2000 characters. The margin absorbs the
# marker line and the fence, exactly as bot.py's encode_state does.
MAX_CONTENT = 1990


@dataclass(frozen=True)
class PendingRequest:
    id: str
    user_id: int
    ign: str
    item: str
    type: str
    requested_at: str
    # Something the officer should see when judging this request, e.g.
    # that the member has previously requested under a different IGN.
    # Empty for the ordinary case.
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "ign": self.ign,
            "item": self.item,
            "type": self.type,
            "requested_at": self.requested_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PendingRequest":
        return cls(
            id=str(raw["id"]),
            user_id=int(raw["user_id"]),
            ign=str(raw["ign"]),
            item=str(raw["item"]),
            type=str(raw["type"]),
            requested_at=str(raw["requested_at"]),
            # Absent in messages written before notes existed.
            note=str(raw.get("note", "")),
        )


@dataclass
class State:
    officer_channel_id: int | None = None
    queue: list[PendingRequest] = field(default_factory=list)
    # Discord user id (as a string, because JSON object keys are strings)
    # -> the IGN they last used. Members type their own IGN, so this is
    # what lets a typo surface as "you used Kobe before" instead of
    # silently crediting a different row.
    igns: dict[str, str] = field(default_factory=dict)


def new_request_id() -> str:
    """A short token identifying one request.

    Written to the ledger and used to detect two officers resolving the
    same request: the second finds it already gone from the queue.
    """
    return secrets.token_hex(4)


def _render(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{STATE_MARKER} -- bot storage, please don't delete this message.\n"
        f"```json\n{body}\n```"
    )


def encode_state(state: State) -> tuple[str, list[PendingRequest]]:
    """Render the state, dropping the oldest requests if it will not fit.

    Returns (content, dropped). The caller MUST tell the officers about
    anything dropped -- silently losing a member's request is the one
    failure mode this whole module exists to prevent, so it is surfaced
    loudly rather than swallowed.
    """
    queue = list(state.queue)
    dropped: list[PendingRequest] = []

    while True:
        content = _render(
            {
                "officer_channel_id": state.officer_channel_id,
                "queue": [r.to_dict() for r in queue],
                "igns": state.igns,
            }
        )
        if len(content) <= MAX_CONTENT or not queue:
            return content, dropped
        dropped.append(queue.pop(0))


def decode_state(content: str) -> State | None:
    """Parse a state message, or None if this isn't one / is corrupt.

    Returning None rather than raising lets the caller scan a channel's
    pins and skip anything that isn't ours, the way bot.restore_state
    does.
    """
    if not content or not content.startswith(STATE_MARKER):
        return None
    start = content.find("```json")
    end = content.rfind("```")
    if start == -1 or end <= start:
        return None
    body = content[start + len("```json") : end].strip()
    try:
        payload = json.loads(body)
        queue = [PendingRequest.from_dict(r) for r in payload.get("queue", [])]
        channel_id = payload.get("officer_channel_id")
        igns = {str(k): str(v) for k, v in dict(payload.get("igns", {})).items()}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return State(
        officer_channel_id=int(channel_id) if channel_id is not None else None,
        queue=queue,
        igns=igns,
    )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_state.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Write the failing test for queue operations**

```python
# append to tests/test_items_state.py
import items_rules


def test_pending_gear_counts_only_that_players_gear_requests():
    state = items_state.State(
        queue=[
            _request("a", ign="Kobe", type_=items_rules.GEAR),
            _request("b", ign="Kobe", type_=items_rules.GEAR),
            _request("c", ign="Kobe", type_=items_rules.SPECIAL),
            _request("d", ign="Dajz", type_=items_rules.GEAR),
        ]
    )
    assert items_state.pending_gear_for(state, "Kobe") == 2
    assert items_state.pending_gear_for(state, "kobe") == 2


def test_find_request_returns_none_when_absent():
    assert items_state.find_request(items_state.State(), "nope") is None


def test_remove_request_takes_it_out_of_the_queue():
    state = items_state.State(queue=[_request("a"), _request("b")])
    removed = items_state.remove_request(state, "a")
    assert removed.id == "a"
    assert [r.id for r in state.queue] == ["b"]


def test_removing_an_already_removed_request_returns_none():
    state = items_state.State(queue=[_request("a")])
    items_state.remove_request(state, "a")
    assert items_state.remove_request(state, "a") is None
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_state.py -k "pending or find or remove" -v`
Expected: FAIL with `AttributeError: module 'items_state' has no attribute 'pending_gear_for'`

- [ ] **Step 7: Implement the queue operations**

```python
# append to items_state.py
import items_rules


def pending_gear_for(state: State, ign: str) -> int:
    """Queued-but-unapproved gear requests for this player."""
    wanted = items_rules.normalize(ign)
    return sum(
        1
        for r in state.queue
        if r.type == items_rules.GEAR and items_rules.normalize(r.ign) == wanted
    )


def find_request(state: State, request_id: str) -> PendingRequest | None:
    for request in state.queue:
        if request.id == request_id:
            return request
    return None


def remove_request(state: State, request_id: str) -> PendingRequest | None:
    """Take a request out of the queue, returning it.

    None means it was already resolved -- which is exactly how a second
    officer clicking the same button is detected.
    """
    found = find_request(state, request_id)
    if found is not None:
        state.queue.remove(found)
    return found
```

Add `from attendance_roster import normalize` re-export usage: `items_rules` already imports `normalize`, so `items_rules.normalize` resolves. No new import needed in `items_state` beyond `items_rules`.

- [ ] **Step 8: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_state.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 9: Commit**

```bash
git add items_state.py tests/test_items_state.py
git commit -m "Persist the request queue in a pinned message"
```

---

## Task 6: Sheet reads

**Files:**
- Create: `items_sheet.py`
- Test: `tests/test_items_sheet.py`

**Interfaces:**
- Consumes: `attendance_sheet.open_spreadsheet` / `read_headers` / `read_players` / `find_column` / `get_or_create_tab` / `SheetStructureError`.
- Produces: `SPECIAL_TAB`, `GEAR_TAB`, `LEDGER_TAB`, `LEDGER_HEADER`, `open_logs_tracker(sheet_id, service_account_json)`, `Snapshot` (frozen dataclass: `roster`, `special_headers`, `gear_headers`, `ledger_rows`, `special_grid`), `read_snapshot(spreadsheet) -> Snapshot`, `holds_special(snapshot, ign, item) -> bool`, `find_row(worksheet, ign, grid=None) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_items_sheet.py
"""Tests for Logs Tracker access, against the shared gspread fakes."""

import pytest

import items_sheet
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_sheet.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'items_sheet'`

- [ ] **Step 3: Write minimal implementation**

```python
# items_sheet.py
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
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_sheet.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add items_sheet.py tests/test_items_sheet.py
git commit -m "Read the Logs Tracker roster, headers and ledger"
```

---

## Task 7: Sheet writes

**Files:**
- Modify: `items_sheet.py`
- Test: `tests/test_items_sheet.py`

**Interfaces:**
- Consumes: everything from Task 6.
- Produces: `LedgerWriteError`, `record_special(spreadsheet, ign, item) -> str`, `record_gear(spreadsheet, ign, item) -> str`, `append_ledger_row(spreadsheet, *, timestamp, ign, item, item_type, officer, user_id, request_id) -> None`, `commit_approval(spreadsheet, *, ign, item, item_type, timestamp, officer, user_id, request_id) -> str`.

Writes go through `worksheet.batch_update([{"range": ..., "values": [[...]]}])`, matching `attendance_sheet.apply_writes`, so tests assert on the payload rather than on how a fake stringifies a value.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_items_sheet.py
import items_rules


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_sheet.py -k record -v`
Expected: FAIL with `AttributeError: module 'items_sheet' has no attribute 'record_special'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to items_sheet.py


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
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_sheet.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add items_sheet.py tests/test_items_sheet.py
git commit -m "Write approvals to the item cell and the ledger"
```

---

## Task 8: Bot skeleton, credentials and officer channel

**Files:**
- Create: `items_bot.py`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `items_state.State` / `encode_state` / `decode_state`, `items_sheet.open_logs_tracker`.
- Produces: `EXIT_NOT_CONFIGURED`, `PANEL_TIMEOUT`, `_SHEET_LOCK`, `_STATE`, `bot`, `today_pht() -> str`, `gear_cap() -> int`, `missing_credentials(env) -> list[str]`, `save_state(channel) -> list[PendingRequest]`, `load_state(channel) -> bool`, `setofficerchannel` command.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_items_bot.py
"""Tests for the item bot's Discord wiring.

No Discord client is started and nothing touches the network; the fakes
below stand in for the handful of discord.py objects the handlers use,
following the local-fakes style of test_attendance_bot.py.
"""

import asyncio

import pytest

import items_bot
import items_state


class FakeMessage:
    def __init__(self, content="", author_is_bot=True, message_id=1):
        self.content = content
        self.id = message_id
        self.pinned = False
        self.edits: list[str] = []

        class _Author:
            bot = author_is_bot

        self.author = _Author()

    async def edit(self, content=None, **kwargs):
        if content is not None:
            self.content = content
            self.edits.append(content)

    async def pin(self):
        self.pinned = True


class FakeChannel:
    def __init__(self, channel_id=99, pins=None):
        self.id = channel_id
        self._pins = list(pins or [])
        self.sent: list[str] = []

    def pins(self, limit=50):
        """An async iterator, matching discord.py 2.7's real signature.

        Modelling this as a coroutine returning a list would let
        `await channel.pins()` pass in tests and blow up in production
        with TypeError, taking restart recovery with it.
        """

        async def _iterator():
            for message in list(self._pins)[:limit]:
                yield message

        return _iterator()

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        message = FakeMessage(content=content or "", message_id=len(self.sent))
        self._pins.append(message)
        return message


@pytest.fixture(autouse=True)
def reset_module_state():
    """items_bot keeps _STATE and _STATE_MESSAGE at module level.

    Without this, a test that saves state leaves _STATE_MESSAGE pointing
    at a previous test's fake message, and the next save_state edits
    that instead of posting to its own channel -- so tests pass or fail
    depending on the order they run in.
    """
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGE = None
    yield
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGE = None


def test_missing_credentials_lists_every_absent_name():
    missing = items_bot.missing_credentials({})
    assert "ITEMS_DISCORD_TOKEN" in missing
    assert "ITEMS_SHEET_ID" in missing
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in missing


def test_missing_credentials_is_empty_when_all_present():
    assert items_bot.missing_credentials(
        {
            "ITEMS_DISCORD_TOKEN": "t",
            "ITEMS_SHEET_ID": "s",
            "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
        }
    ) == []


def test_exit_code_matches_the_supervisors_leave_it_stopped_code():
    assert items_bot.EXIT_NOT_CONFIGURED == 78


def test_gear_cap_defaults_to_three(monkeypatch):
    monkeypatch.delenv("ITEMS_GEAR_DAILY_CAP", raising=False)
    assert items_bot.gear_cap() == 3


def test_gear_cap_is_overridable(monkeypatch):
    monkeypatch.setenv("ITEMS_GEAR_DAILY_CAP", "5")
    assert items_bot.gear_cap() == 5


def test_a_nonsense_gear_cap_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ITEMS_GEAR_DAILY_CAP", "banana")
    assert items_bot.gear_cap() == 3


def test_a_module_level_lock_exists():
    assert isinstance(items_bot._SHEET_LOCK, asyncio.Lock)


def test_save_then_load_restores_the_queue():
    channel = FakeChannel()
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = [
        items_state.PendingRequest(
            id="aaa", user_id=1, ign="Kobe", item="Asta's Heart",
            type="Special", requested_at="2026-08-07 09:00:00",
        )
    ]

    asyncio.run(items_bot.save_state(channel))
    items_bot._STATE.queue = []
    asyncio.run(items_bot.load_state(channel))

    assert [r.id for r in items_bot._STATE.queue] == ["aaa"]


def test_dropped_requests_are_removed_from_memory_too():
    """Storage and memory must not disagree about what is queued."""
    channel = FakeChannel()
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = [
        items_state.PendingRequest(
            id=f"id{n:03d}", user_id=1, ign="Kobe",
            item="A Very Long Item Name Indeed",
            type="Gear", requested_at="2026-08-07 09:00:00",
        )
        for n in range(300)
    ]

    dropped = asyncio.run(items_bot.save_state(channel))

    assert dropped
    surviving = {r.id for r in items_bot._STATE.queue}
    assert not any(d.id in surviving for d in dropped)


def test_saving_twice_edits_the_same_message_rather_than_posting_again():
    channel = FakeChannel()
    items_bot._STATE.officer_channel_id = channel.id
    items_bot._STATE.queue = []

    asyncio.run(items_bot.save_state(channel))
    asyncio.run(items_bot.save_state(channel))

    assert len(channel.sent) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'items_bot'`

- [ ] **Step 3: Write minimal implementation**

```python
# items_bot.py
"""Discord bot for guild item requests and officer distribution.

A third process alongside the timer and the attendance bot, with its own
Discord token and its own spreadsheet, so nothing it does can affect
either of them. See supervisor.py for why all three share one Render
service.

Authorization is the private officer channel itself: !distribute is
accepted only there, and a button attached to a message in that channel
can only be pressed by someone Discord already lets see the channel.
There is no role configuration to drift.
"""

import asyncio
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

import items_rules
import items_sheet
import items_state

load_dotenv()

EXIT_NOT_CONFIGURED = 78

REQUIRED_ENV = ("ITEMS_DISCORD_TOKEN", "ITEMS_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")

# How long a !distribute panel accepts clicks. The queue outlives the
# panel; expiry only means the officer re-runs the command.
PANEL_TIMEOUT = 900  # 15 minutes

# Serializes every read-then-write pair. Two officers approving at once
# would otherwise both read "2 used today" and both write, yielding 4.
_SHEET_LOCK = asyncio.Lock()

_STATE = items_state.State()

# The pinned message holding _STATE, cached so save_state edits it
# instead of posting a new one on every change.
_STATE_MESSAGE: discord.Message | None = None

_SPREADSHEET = None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def missing_credentials(env: dict) -> list[str]:
    return [name for name in REQUIRED_ENV if not env.get(name)]


def today_pht() -> str:
    """Today's date in Manila, as the ledger writes it."""
    return items_rules.pht_day(items_rules.format_timestamp(items_rules.now_pht()))


def gear_cap() -> int:
    """The daily gear limit, from the environment.

    A malformed value falls back to the default rather than crashing the
    bot: a typo in a Render env var should not take the bot down.
    """
    try:
        return int(os.getenv("ITEMS_GEAR_DAILY_CAP", ""))
    except ValueError:
        return items_rules.DEFAULT_GEAR_DAILY_CAP


async def save_state(channel) -> list[items_state.PendingRequest]:
    """Write _STATE into the pinned message. Returns anything dropped.

    Anything the encoder had to drop to fit is removed from _STATE.queue
    too. Without that, the dropped requests survive in memory, still
    appear in panels, and force the encoder to drop them again on every
    single save -- while a restart resurrects a state message that never
    contained them. Memory and storage must agree.
    """
    global _STATE_MESSAGE
    content, dropped = items_state.encode_state(_STATE)
    if dropped:
        gone = {r.id for r in dropped}
        _STATE.queue = [r for r in _STATE.queue if r.id not in gone]

    if _STATE_MESSAGE is not None:
        try:
            await _STATE_MESSAGE.edit(content=content)
            return dropped
        except discord.HTTPException:
            _STATE_MESSAGE = None

    _STATE_MESSAGE = await channel.send(content)
    try:
        await _STATE_MESSAGE.pin()
    except discord.HTTPException:
        # Pinning needs Manage Messages. Without it the message still
        # works -- load_state scans history too -- so this is not fatal.
        pass
    return dropped


async def load_state(channel) -> bool:
    """Restore _STATE from the channel's pinned messages.

    Returns True if a state message was found.

    channel.pins() is an ASYNC ITERATOR in discord.py 2.7, not a
    coroutine returning a list -- `await channel.pins()` raises
    TypeError. It must be consumed with `async for`.
    """
    global _STATE_MESSAGE
    candidates = [message async for message in channel.pins(limit=50)]
    for message in candidates:
        decoded = items_state.decode_state(message.content)
        if decoded is None:
            continue
        _STATE.officer_channel_id = decoded.officer_channel_id or channel.id
        _STATE.queue = decoded.queue
        _STATE.igns = decoded.igns
        _STATE_MESSAGE = message
        return True
    return False
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Add the setofficerchannel command and its test**

```python
# append to items_bot.py


def _embed(title: str, description: str, colour: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=colour)


def ok_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"✅ {title}", description, 0x2ECC71)


def error_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"❌ {title}", description, 0xE74C3C)


def is_officer_channel(channel_id: int) -> bool:
    return (
        _STATE.officer_channel_id is not None
        and channel_id == _STATE.officer_channel_id
    )


@bot.command(name="setofficerchannel")
@commands.has_permissions(administrator=True)
async def setofficerchannel_cmd(ctx):
    """Record this channel as the officers' channel."""
    _STATE.officer_channel_id = ctx.channel.id
    await save_state(ctx.channel)
    await ctx.send(
        embed=ok_embed(
            "Officer channel set",
            f"`!distribute` now works in {ctx.channel.mention}, and the bot "
            "keeps its request queue in a pinned message here. Don't delete it.",
        )
    )
```

```python
# append to tests/test_items_bot.py


def test_is_officer_channel_only_matches_the_recorded_channel():
    items_bot._STATE.officer_channel_id = 99
    assert items_bot.is_officer_channel(99)
    assert not items_bot.is_officer_channel(100)


def test_is_officer_channel_is_false_before_setup():
    items_bot._STATE.officer_channel_id = None
    assert not items_bot.is_officer_channel(99)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 7: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Add the item bot skeleton and officer channel setup"
```

---

## Task 9: The `!request` command

**Files:**
- Modify: `items_bot.py`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `items_rules.parse_request` / `check_eligibility`, `items_sheet.read_snapshot` / `holds_special`, `items_state.pending_gear_for`.
- Produces: `RequestOutcome` (frozen dataclass: `accepted: bool`, `message: str`, `request: PendingRequest | None`), `evaluate_request(argument, user_id, snapshot, state, *, cap, today) -> RequestOutcome`, `request_cmd` command.

`evaluate_request` is pure and holds all the decision logic; the command is thin I/O around it. That is what makes the request path testable without a Discord client.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_items_bot.py
import items_rules
import items_sheet

SPECIAL_HEADER_ROW = ["Player Name", "Asta's Heart", "Amentis' Foot"]

# Kobe already holds Asta's Heart; nobody else does.
SPECIAL_GRID_ROWS = [
    SPECIAL_HEADER_ROW,
    ["Kobe", "TRUE", "FALSE"],
    ["Dajz", "FALSE", "FALSE"],
    ["chinchong ni Mumu", "FALSE", "FALSE"],
]


def snapshot_with(ledger_rows=None, special_grid=None):
    return items_sheet.Snapshot(
        roster=["Kobe", "Dajz", "chinchong ni Mumu"],
        special_headers=SPECIAL_HEADER_ROW,
        gear_headers=["Player Name", "Asta's Belt", "Benji's Heart"],
        ledger_rows=ledger_rows
        if ledger_rows is not None
        else [
            ["2026-08-07 09:00:00", "Kobe", "Asta's Belt", "Gear", "O", "1", "aaa"],
            ["2026-08-07 10:00:00", "Kobe", "Benji's Heart", "Gear", "O", "1", "bbb"],
        ],
        special_grid=special_grid if special_grid is not None else SPECIAL_GRID_ROWS,
    )


SNAPSHOT = snapshot_with()


def _evaluate(argument, state=None, user_id=1, snapshot=None):
    return items_bot.evaluate_request(
        argument,
        user_id,
        snapshot if snapshot is not None else SNAPSHOT,
        state if state is not None else items_state.State(),
        cap=3,
        today="2026-08-07",
    )


def test_a_valid_special_request_is_accepted():
    outcome = _evaluate("Asta's Heart Dajz")
    assert outcome.accepted
    assert outcome.request.item == "Asta's Heart"
    assert outcome.request.type == items_rules.SPECIAL


def test_a_multi_word_ign_is_accepted():
    outcome = _evaluate("Asta's Heart chinchong ni Mumu")
    assert outcome.accepted
    assert outcome.request.ign == "chinchong ni Mumu"


def test_a_special_the_player_already_holds_is_refused():
    outcome = _evaluate("Asta's Heart Kobe")
    assert not outcome.accepted
    assert "already" in outcome.message.lower()


def test_a_gear_request_at_the_cap_is_refused_before_officers_see_it():
    ledger = SNAPSHOT.ledger_rows + [
        ["2026-08-07 11:00:00", "Kobe", "Asta's Belt", "Gear", "O", "1", "ccc"]
    ]
    outcome = _evaluate("Asta's Belt Kobe", snapshot=snapshot_with(ledger_rows=ledger))
    assert not outcome.accepted


def test_pending_gear_requests_count_toward_the_cap():
    state = items_state.State(
        queue=[
            items_state.PendingRequest(
                id="x", user_id=1, ign="Kobe", item="Asta's Belt",
                type=items_rules.GEAR, requested_at="2026-08-07 10:30:00",
            )
        ]
    )
    outcome = _evaluate("Asta's Belt Kobe", state=state)
    assert not outcome.accepted


def test_a_duplicate_pending_request_is_refused():
    state = items_state.State(
        queue=[
            items_state.PendingRequest(
                id="x", user_id=1, ign="Dajz", item="Asta's Heart",
                type=items_rules.SPECIAL, requested_at="2026-08-07 10:30:00",
            )
        ]
    )
    outcome = _evaluate("Asta's Heart Dajz", state=state)
    assert not outcome.accepted
    assert "pending" in outcome.message.lower()


def test_an_unparseable_request_is_refused_with_the_reason():
    outcome = _evaluate("Asta's Heart Nobody")
    assert not outcome.accepted
    assert "Nobody" in outcome.message


def test_an_ign_differing_from_last_time_is_noted_not_refused():
    """Requesting for an alt is legitimate; the officer judges it."""
    state = items_state.State(igns={"1": "Kobe"})
    outcome = _evaluate("Asta's Heart Dajz", state=state, user_id=1)
    assert outcome.accepted
    assert "Kobe" in outcome.request.note


def test_the_same_ign_as_last_time_carries_no_note():
    state = items_state.State(igns={"1": "Dajz"})
    outcome = _evaluate("Asta's Heart Dajz", state=state, user_id=1)
    assert outcome.accepted
    assert outcome.request.note == ""


def test_a_duplicate_is_refused_even_from_a_different_account():
    """Keyed on IGN, not on who asked."""
    state = items_state.State(
        queue=[
            items_state.PendingRequest(
                id="x", user_id=999, ign="Dajz", item="Asta's Heart",
                type=items_rules.SPECIAL, requested_at="2026-08-07 10:30:00",
            )
        ]
    )
    outcome = _evaluate("Asta's Heart Dajz", state=state, user_id=1)
    assert not outcome.accepted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -q`
Expected: FAIL with `AttributeError: module 'items_bot' has no attribute 'evaluate_request'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to items_bot.py
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestOutcome:
    accepted: bool
    message: str
    request: items_state.PendingRequest | None = None


def evaluate_request(
    argument: str,
    user_id: int,
    snapshot: items_sheet.Snapshot,
    state: items_state.State,
    *,
    cap: int,
    today: str,
) -> RequestOutcome:
    """Decide a !request without touching Discord or the network.

    Pure: the snapshot already carries the special-log checkbox grid, so
    every question this asks is answered from values passed in. That is
    what makes the whole request path testable without a network.
    """
    try:
        parsed = items_rules.parse_request(
            argument, snapshot.roster, snapshot.special_headers, snapshot.gear_headers
        )
    except (items_rules.RequestParseError, items_rules.ItemLookupError) as exc:
        return RequestOutcome(accepted=False, message=str(exc))

    # A member requesting under a different IGN than last time is NOT
    # refused -- requesting for an alt is legitimate. It is flagged for
    # the officer instead, who is the one with the standing to judge it.
    # Blocking here would punish the honest case to catch a typo that
    # the roster check has already largely prevented.
    note = ""
    remembered = state.igns.get(str(user_id))
    if remembered and items_rules.normalize(remembered) != items_rules.normalize(parsed.ign):
        note = f"previously requested as {remembered}"

    # Keyed on IGN, not on the requesting account: the same item must
    # not sit in the queue twice for one player, whoever asked.
    for queued in state.queue:
        if (
            items_rules.normalize(queued.ign) == items_rules.normalize(parsed.ign)
            and items_rules.normalize(queued.item) == items_rules.normalize(parsed.item.name)
        ):
            return RequestOutcome(
                accepted=False,
                message=f"**{parsed.item.name}** is already pending for **{parsed.ign}**.",
            )

    eligibility = items_rules.check_eligibility(
        parsed.item.type,
        parsed.ign,
        snapshot.ledger_rows,
        today,
        already_has_special=items_sheet.holds_special(
            snapshot, parsed.ign, parsed.item.name
        ),
        pending_gear=items_state.pending_gear_for(state, parsed.ign),
        cap=cap,
    )
    if not eligibility.allowed:
        return RequestOutcome(
            accepted=False, message=f"**{parsed.ign}** {eligibility.reason}."
        )

    return RequestOutcome(
        accepted=True,
        message=(
            f"Requested **{parsed.item.name}** ({parsed.item.type}) for "
            f"**{parsed.ign}**. An officer will review it."
        ),
        request=items_state.PendingRequest(
            id=items_state.new_request_id(),
            user_id=user_id,
            ign=parsed.ign,
            item=parsed.item.name,
            type=parsed.item.type,
            requested_at=items_rules.format_timestamp(items_rules.now_pht()),
            note=note,
        ),
    )


@bot.command(name="request")
async def request_cmd(ctx, *, argument: str = ""):
    """Ask an officer for an item."""
    if _STATE.officer_channel_id is None:
        await ctx.send(
            embed=error_embed(
                "Not set up yet",
                "An admin must run `!setofficerchannel` in the officers' "
                "channel before requests can be taken.",
            )
        )
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        outcome = evaluate_request(
            argument,
            ctx.author.id,
            snapshot,
            _STATE,
            cap=gear_cap(),
            today=today_pht(),
        )

        if not outcome.accepted:
            await ctx.send(embed=error_embed("Request refused", outcome.message))
            return

        _STATE.queue.append(outcome.request)
        _STATE.igns[str(ctx.author.id)] = outcome.request.ign

        channel = bot.get_channel(_STATE.officer_channel_id)
        dropped = await save_state(channel) if channel else []

    await ctx.send(embed=ok_embed("Request queued", outcome.message))
    if dropped and channel:
        names = ", ".join(f"{d.item} ({d.ign})" for d in dropped)
        await channel.send(
            embed=error_embed(
                "Queue overflowed",
                f"The queue no longer fits in one message. These oldest "
                f"requests were dropped and must be re-requested: {names}",
            )
        )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Accept member item requests into the queue"
```

---

## Task 10: The `!distribute` panel

**Files:**
- Modify: `items_bot.py`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: everything from Task 9, plus `items_sheet.commit_approval`.
- Produces: `MAX_PANEL_OPTIONS`, `panel_lines(requests, snapshot, cap, today, start=1) -> list[str]`, `build_panel_embed(requests, snapshot, cap, today, start=1) -> discord.Embed`, `pages(requests) -> list[list[PendingRequest]]`, `send_panels(destination, snapshot, requests)`, `DistributePanel(discord.ui.View)`, `refresh_panel(interaction, start)`, `approve(request_id, officer_name) -> str`, `deny(request_id) -> str`, `distribute_cmd` command.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_items_bot.py


def _queued(request_id, ign, item, type_):
    return items_state.PendingRequest(
        id=request_id, user_id=1, ign=ign, item=item, type=type_,
        requested_at="2026-08-07 09:00:00",
    )


def test_panel_lines_number_each_request_and_show_its_status():
    lines = items_bot.panel_lines(
        [
            _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
            _queued("b", "Kobe", "Asta's Belt", items_rules.GEAR),
        ],
        SNAPSHOT,
        cap=3,
        today="2026-08-07",
    )
    assert lines[0].startswith("1.")
    assert "Dajz" in lines[0] and "Asta's Heart" in lines[0]
    assert "2/3" in lines[1], "a gear line shows how many the player used today"


def test_panel_lines_flag_a_player_at_the_cap():
    ledger = SNAPSHOT.ledger_rows + [
        ["2026-08-07 11:00:00", "Kobe", "Asta's Belt", "Gear", "O", "1", "ccc"]
    ]
    lines = items_bot.panel_lines(
        [_queued("b", "Kobe", "Asta's Belt", items_rules.GEAR)],
        snapshot_with(ledger_rows=ledger), cap=3, today="2026-08-07",
    )
    assert "⚠️" in lines[0]


def test_panel_lines_flag_a_special_the_player_already_holds():
    """Kobe already has Asta's Heart in SPECIAL_GRID_ROWS."""
    lines = items_bot.panel_lines(
        [_queued("a", "Kobe", "Asta's Heart", items_rules.SPECIAL)],
        SNAPSHOT, cap=3, today="2026-08-07",
    )
    assert "already has it" in lines[0]


def test_panel_lines_show_a_requests_note():
    request = items_state.PendingRequest(
        id="a", user_id=1, ign="Dajz", item="Asta's Heart",
        type=items_rules.SPECIAL, requested_at="2026-08-07 09:00:00",
        note="previously requested as Kobe",
    )
    lines = items_bot.panel_lines([request], SNAPSHOT, cap=3, today="2026-08-07")
    assert "previously requested as Kobe" in lines[0]


def test_panel_lines_number_from_the_page_start():
    lines = items_bot.panel_lines(
        [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)],
        SNAPSHOT, cap=3, today="2026-08-07", start=26,
    )
    assert lines[0].startswith("**26.")


def test_pages_chunks_the_queue_so_every_request_is_reachable():
    queue = [_queued(f"id{n}", "Dajz", "Asta's Heart", items_rules.SPECIAL) for n in range(60)]
    chunks = items_bot.pages(queue)
    assert [len(c) for c in chunks] == [25, 25, 10]
    assert sum(len(c) for c in chunks) == 60


def test_pages_of_an_empty_queue_is_one_empty_page():
    assert items_bot.pages([]) == [[]]


def test_a_panel_remembers_which_page_it_shows():
    """Page 2 must redraw as page 2, not be replaced by page 1."""
    queue = [_queued(f"id{n}", "Dajz", "Asta's Heart", items_rules.SPECIAL) for n in range(30)]
    panel = items_bot.DistributePanel(items_bot.pages(queue)[1], start=26)
    assert panel.start == 26


def test_an_empty_queue_says_so():
    embed = items_bot.build_panel_embed([], SNAPSHOT, cap=3, today="2026-08-07")
    assert "no pending" in embed.description.lower()


def test_deny_removes_the_request_and_writes_nothing():
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    message = asyncio.run(items_bot.deny("a"))
    assert items_bot._STATE.queue == []
    assert "Dajz" in message


def test_denying_an_already_resolved_request_reports_it():
    items_bot._STATE.queue = []
    message = asyncio.run(items_bot.deny("gone"))
    assert "already" in message.lower()


def test_approving_an_already_resolved_request_writes_nothing(monkeypatch):
    items_bot._STATE.queue = []
    calls = []
    monkeypatch.setattr(
        items_sheet, "commit_approval", lambda *a, **k: calls.append(k)
    )
    message = asyncio.run(items_bot.approve("gone", "Keith"))
    assert calls == []
    assert "already" in message.lower()


def test_approve_commits_and_removes_the_request(monkeypatch):
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    calls = []
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(
        items_sheet, "commit_approval",
        lambda spreadsheet, **kwargs: calls.append(kwargs) or "B3",
    )
    monkeypatch.setattr(items_bot, "save_state", _noop_save)

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert len(calls) == 1
    assert calls[0]["ign"] == "Dajz"
    assert calls[0]["officer"] == "Keith"
    assert items_bot._STATE.queue == []
    assert "Dajz" in message


def test_approve_rechecks_the_cap_and_refuses_a_stale_request(monkeypatch):
    """The second cap check is the point of this test.

    The request passed the check when it was queued. By the time an
    officer clicks, the player has been given their third gear log by
    hand, so the approval must be refused even though the request is
    still sitting in the queue.
    """
    items_bot._STATE.queue = [_queued("a", "Kobe", "Asta's Belt", items_rules.GEAR)]
    full_ledger = SNAPSHOT.ledger_rows + [
        ["2026-08-07 11:00:00", "Kobe", "Benji's Heart", "Gear", "O", "1", "ccc"]
    ]
    monkeypatch.setattr(
        items_sheet, "read_snapshot",
        lambda spreadsheet: snapshot_with(ledger_rows=full_ledger),
    )
    calls = []
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(items_bot, "save_state", _noop_save)

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert calls == [], "no write when the cap is already reached"
    assert items_bot._STATE.queue, "a refused approval leaves the request queued"
    assert "limit" in message.lower()


def test_a_failed_sheet_write_leaves_the_request_queued(monkeypatch):
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)

    def boom(*args, **kwargs):
        raise RuntimeError("sheets is down")

    monkeypatch.setattr(items_sheet, "commit_approval", boom)
    monkeypatch.setattr(items_bot, "save_state", _noop_save)

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert items_bot._STATE.queue, "nothing may be lost when the sheet fails"
    assert "sheets is down" in message


def test_a_partial_write_dequeues_and_hands_over_the_row(monkeypatch):
    """The cell is written; a retry would double-count.

    So the request must NOT stay queued, and the officers must be told
    exactly what to paste into the ledger.
    """
    items_bot._STATE.queue = [_queued("a", "Kobe", "Asta's Belt", items_rules.GEAR)]
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: SNAPSHOT)
    monkeypatch.setattr(items_bot, "save_state", _noop_save)

    row = ["2026-08-07 14:00:00", "Kobe", "Asta's Belt", "Gear", "Keith", "1", "a"]

    def partial(*args, **kwargs):
        raise items_sheet.LedgerWriteError("B2", row, RuntimeError("ledger is down"))

    monkeypatch.setattr(items_sheet, "commit_approval", partial)

    message = asyncio.run(items_bot.approve("a", "Keith"))

    assert items_bot._STATE.queue == [], "a written cell must not be re-approvable"
    assert "B2" in message
    assert "Asta's Belt" in message
    assert "do not approve this again" in message.lower()


async def _noop_save(channel=None):
    return []
```

Add near the top of the test file, so `_noop_save` is defined before use at import time is not required (it is referenced only inside test bodies):

```python
# no extra import needed; _noop_save is defined at module level above
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -k "panel or approve or deny" -v`
Expected: FAIL with `AttributeError: module 'items_bot' has no attribute 'panel_lines'`

- [ ] **Step 3: Implement the panel rendering and the resolve functions**

```python
# append to items_bot.py

# Discord allows at most 25 options in a select menu.
MAX_PANEL_OPTIONS = 25


def panel_lines(
    requests: list[items_state.PendingRequest],
    snapshot: items_sheet.Snapshot,
    cap: int,
    today: str,
    start: int = 1,
) -> list[str]:
    """One display line per pending request, with its current standing.

    The standing is recomputed at render time, not stored: an officer
    needs to see the position as it is now, which may differ from when
    the member requested.
    """
    lines = []
    for number, request in enumerate(requests, start=start):
        if request.type == items_rules.GEAR:
            used = items_rules.gear_used_today(snapshot.ledger_rows, request.ign, today)
            flag = "⚠️" if used >= cap else "✅"
            status = f"{flag} {used}/{cap} today"
        elif items_sheet.holds_special(snapshot, request.ign, request.item):
            status = "⚠️ already has it"
        else:
            status = "✅ eligible"
        line = (
            f"**{number}. {request.ign}** — {request.item}  "
            f"`[{request.type}]`  {status}"
        )
        if request.note:
            line += f"\n     ⚠️ {request.note}"
        lines.append(line)
    return lines


def build_panel_embed(
    requests: list[items_state.PendingRequest],
    snapshot: items_sheet.Snapshot,
    cap: int,
    today: str,
    start: int = 1,
) -> discord.Embed:
    if not requests:
        return _embed("📦 Pending Item Requests", "There are no pending requests.", 0x95A5A6)
    body = "\n".join(panel_lines(requests, snapshot, cap, today, start))
    return _embed("📦 Pending Item Requests", body, 0x3498DB)


async def deny(request_id: str) -> str:
    """Drop a request. Writes nothing to any tab."""
    async with _SHEET_LOCK:
        removed = items_state.remove_request(_STATE, request_id)
        if removed is None:
            return "That request was already handled by another officer."
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)
    return f"Denied **{removed.item}** for **{removed.ign}**. Nothing was written to the sheet."


async def approve(request_id: str, officer_name: str) -> str:
    """Write the item to the sheet and the ledger, then drop the request.

    The whole sequence -- re-read, re-check the cap, write -- happens
    under _SHEET_LOCK. Splitting it would let two officers both read
    "2 used today" and both write.
    """
    async with _SHEET_LOCK:
        request = items_state.find_request(_STATE, request_id)
        if request is None:
            return "That request was already handled by another officer."

        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            return f"Could not read the sheet, so nothing was written: {exc}"

        eligibility = items_rules.check_eligibility(
            request.type,
            request.ign,
            snapshot.ledger_rows,
            today_pht(),
            already_has_special=items_sheet.holds_special(
                snapshot, request.ign, request.item
            ),
            cap=gear_cap(),
        )
        if not eligibility.allowed:
            return (
                f"Not approved: **{request.ign}** {eligibility.reason}. "
                "The request is still in the queue."
            )

        try:
            await asyncio.to_thread(
                lambda: items_sheet.commit_approval(
                    _SPREADSHEET,
                    ign=request.ign,
                    item=request.item,
                    item_type=request.type,
                    timestamp=items_rules.format_timestamp(items_rules.now_pht()),
                    officer=officer_name,
                    user_id=request.user_id,
                    request_id=request.id,
                )
            )
        except items_sheet.LedgerWriteError as exc:
            # The item cell IS written. Retrying would double-count a
            # gear increment and could never succeed for a special log,
            # so drop the request and hand the officers the exact row.
            items_state.remove_request(_STATE, request_id)
            channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
            if channel is not None:
                await save_state(channel)
            pasteable = " | ".join(exc.row)
            return (
                f"⚠️ **{request.item}** was given to **{request.ign}** "
                f"(cell {exc.address} is updated), but the Distribution Log "
                f"row could not be written: {exc}\n"
                f"Do NOT approve this again — add this row to "
                f"`{items_sheet.LEDGER_TAB}` by hand:\n```\n{pasteable}\n```"
            )
        except Exception as exc:
            return f"Sheet write failed, request kept in the queue: {exc}"

        items_state.remove_request(_STATE, request_id)
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)

    return f"Approved **{request.item}** for **{request.ign}**."
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Add the view and the command**

```python
# append to items_bot.py


class DistributePanel(discord.ui.View):
    """Select a request, then approve or deny it.

    A select plus two buttons rather than a pair of buttons per request:
    Discord allows five action rows of five components, which would cap
    the panel at five requests. A select handles 25.
    """

    def __init__(self, requests: list[items_state.PendingRequest], *, start: int = 1):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.selected: str | None = None
        # The page this panel shows, so refresh_panel redraws THIS page.
        self.start = start
        # Set by the sender so on_timeout can edit the panel.
        self.message: discord.Message | None = None

        options = [
            discord.SelectOption(
                label=f"{n}. {r.ign} — {r.item}"[:100],
                value=r.id,
                description=f"{r.type} · requested {r.requested_at}"[:100],
            )
            for n, r in enumerate(requests[:MAX_PANEL_OPTIONS], start=start)
        ]
        self.picker = discord.ui.Select(placeholder="Choose a request…", options=options)
        self.picker.callback = self._on_pick
        self.add_item(self.picker)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The private channel is the authorization gate.

        Discord already stops anyone who cannot see the channel from
        clicking. This re-checks against the CURRENTLY recorded officer
        channel -- not the one captured when the panel was built -- so
        that moving the officer channel with !setofficerchannel
        immediately makes panels left behind in the old channel inert.
        """
        if not is_officer_channel(interaction.channel_id):
            await interaction.response.send_message(
                "This panel only works in the current officers' channel.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        """Say the panel expired instead of failing silently.

        Once the timeout elapses discord.py stops dispatching component
        interactions, so without this an officer clicking an old panel
        gets Discord's generic "interaction failed" and no explanation.
        The queue itself is untouched -- only this view is dead.
        """
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏳ This panel expired. Run `!distribute` for a fresh one.",
                view=self,
            )
        except discord.HTTPException:
            pass

    async def _on_pick(self, interaction: discord.Interaction):
        self.selected = self.picker.values[0]
        await interaction.response.defer()

    async def _require_selection(self, interaction: discord.Interaction) -> bool:
        if self.selected is None:
            await interaction.response.send_message(
                "Pick a request from the dropdown first.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, _button):
        if not await self._require_selection(interaction):
            return
        await interaction.response.defer()
        result = await approve(self.selected, interaction.user.display_name)
        self.selected = None
        await interaction.followup.send(result)
        await refresh_panel(interaction, self.start)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_button(self, interaction: discord.Interaction, _button):
        if not await self._require_selection(interaction):
            return
        await interaction.response.defer()
        result = await deny(self.selected)
        self.selected = None
        await interaction.followup.send(result)
        await refresh_panel(interaction, self.start)


def pages(requests: list[items_state.PendingRequest]) -> list[list[items_state.PendingRequest]]:
    """Split the queue into panel-sized chunks.

    Discord allows at most 25 options in a select, so a queue longer
    than that needs more than one panel. Chunking and posting one panel
    per chunk is real pagination: every request is reachable. Showing
    only the first 25 and asking officers to re-run the command would
    not be -- re-running shows the same 25.
    """
    return [
        requests[i : i + MAX_PANEL_OPTIONS]
        for i in range(0, len(requests), MAX_PANEL_OPTIONS)
    ] or [[]]


async def send_panels(destination, snapshot, requests) -> None:
    """Post one panel per page of the queue."""
    cap = gear_cap()
    today = today_pht()
    chunks = pages(requests)
    for index, chunk in enumerate(chunks):
        start = index * MAX_PANEL_OPTIONS + 1
        embed = build_panel_embed(chunk, snapshot, cap, today, start=start)
        if len(chunks) > 1:
            embed.set_footer(text=f"Page {index + 1} of {len(chunks)}")
        view = DistributePanel(chunk, start=start) if chunk else None
        message = await destination.send(embed=embed, view=view)
        if view is not None:
            view.message = message


async def refresh_panel(interaction: discord.Interaction, start: int) -> None:
    """Redraw one page of the queue in place after a request resolves.

    `start` is the page this panel is showing, so page 2 stays page 2
    rather than being replaced by page 1's contents. Other pages go
    stale, which is harmless: approve() and deny() both re-check the
    queue, so a click on a stale entry reports that it was already
    handled rather than acting twice.
    """
    try:
        snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
    except Exception:
        return
    offset = start - 1
    requests = list(_STATE.queue)[offset : offset + MAX_PANEL_OPTIONS]
    embed = build_panel_embed(requests, snapshot, gear_cap(), today_pht(), start=start)
    view = DistributePanel(requests, start=start) if requests else None
    await interaction.message.edit(embed=embed, view=view)
    if view is not None:
        view.message = interaction.message


@bot.command(name="distribute")
async def distribute_cmd(ctx):
    """Show the pending requests with approve/deny controls."""
    if not is_officer_channel(ctx.channel.id):
        return  # silently ignored outside the officers' channel

    try:
        snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
    except Exception as exc:
        await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
        return

    await send_panels(ctx, snapshot, list(_STATE.queue))
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS — all item tests plus the existing attendance suite unchanged

- [ ] **Step 7: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Add the officer distribution panel"
```

---

## Task 11: Member helper commands and startup

**Files:**
- Modify: `items_bot.py`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `cancelrequest_cmd`, `myrequests_cmd`, `itemhelp_cmd`, `on_ready`, `on_command_error`, `main()`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_items_bot.py


def test_requests_for_user_returns_only_that_users_requests():
    items_bot._STATE.queue = [
        _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
        _queued("b", "Kobe", "Asta's Belt", items_rules.GEAR),
    ]
    items_bot._STATE.queue[1] = items_state.PendingRequest(
        id="b", user_id=2, ign="Kobe", item="Asta's Belt",
        type=items_rules.GEAR, requested_at="2026-08-07 09:00:00",
    )
    mine = items_bot.requests_for_user(items_bot._STATE, 1)
    assert [r.id for r in mine] == ["a"]


def test_cancellable_picks_the_only_pending_request():
    items_bot._STATE.queue = [_queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL)]
    found, error = items_bot.cancellable(items_bot._STATE, 1, "")
    assert found.id == "a"
    assert error is None


def test_cancellable_needs_a_name_when_several_are_pending():
    items_bot._STATE.queue = [
        _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
        _queued("b", "Dajz", "Amentis' Foot", items_rules.SPECIAL),
    ]
    found, error = items_bot.cancellable(items_bot._STATE, 1, "")
    assert found is None
    assert "Asta's Heart" in error


def test_cancellable_matches_by_item_name():
    items_bot._STATE.queue = [
        _queued("a", "Dajz", "Asta's Heart", items_rules.SPECIAL),
        _queued("b", "Dajz", "Amentis' Foot", items_rules.SPECIAL),
    ]
    found, error = items_bot.cancellable(items_bot._STATE, 1, "amentis' foot")
    assert found.id == "b"


def test_cancellable_reports_when_nothing_is_pending():
    items_bot._STATE.queue = []
    found, error = items_bot.cancellable(items_bot._STATE, 1, "")
    assert found is None
    assert "no pending" in error.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -k "cancellable or requests_for" -v`
Expected: FAIL with `AttributeError: module 'items_bot' has no attribute 'requests_for_user'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to items_bot.py


def requests_for_user(state: items_state.State, user_id: int) -> list[items_state.PendingRequest]:
    return [r for r in state.queue if r.user_id == user_id]


def cancellable(
    state: items_state.State, user_id: int, item_query: str
) -> tuple[items_state.PendingRequest | None, str | None]:
    """Which of this member's pending requests !cancelrequest means.

    Returns (request, error_message); exactly one is None.
    """
    mine = requests_for_user(state, user_id)
    if not mine:
        return None, "You have no pending requests."

    query = item_query.strip()
    if not query:
        if len(mine) == 1:
            return mine[0], None
        names = ", ".join(f"`{r.item}`" for r in mine)
        return None, f"You have several pending: {names}. Say which: `!cancelrequest <item name>`"

    wanted = items_rules.normalize(query)
    for request in mine:
        if items_rules.normalize(request.item) == wanted:
            return request, None
    return None, f"You have no pending request for {query!r}."


@bot.command(name="cancelrequest")
async def cancelrequest_cmd(ctx, *, item_query: str = ""):
    """Withdraw your own pending request."""
    async with _SHEET_LOCK:
        request, error = cancellable(_STATE, ctx.author.id, item_query)
        if error is not None:
            await ctx.send(embed=error_embed("Nothing cancelled", error))
            return
        items_state.remove_request(_STATE, request.id)
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)
    await ctx.send(
        embed=ok_embed("Request cancelled", f"Withdrew **{request.item}** for **{request.ign}**.")
    )


@bot.command(name="myrequests")
async def myrequests_cmd(ctx):
    """List your pending requests."""
    mine = requests_for_user(_STATE, ctx.author.id)
    if not mine:
        await ctx.send(embed=ok_embed("Nothing pending", "You have no pending requests."))
        return
    body = "\n".join(f"• **{r.item}** for **{r.ign}** — requested {r.requested_at}" for r in mine)
    await ctx.send(embed=_embed("📋 Your Pending Requests", body, 0x3498DB))


@bot.command(name="itemhelp")
async def itemhelp_cmd(ctx):
    """Explain the commands and the rules."""
    await ctx.send(
        embed=_embed(
            "📦 Item Requests",
            "**`!request <item name> <IGN>`** — ask for an item. "
            "Example: `!request Asta's Heart Kobe`\n"
            "**`!myrequests`** — see what you have pending\n"
            "**`!cancelrequest [item name]`** — withdraw a request\n\n"
            "**Rules**\n"
            "• Special logs: one per player, ever.\n"
            f"• Gear logs: {gear_cap()} per player per day, resetting at "
            "midnight (Manila time).\n\n"
            "Your IGN must match your row in the Logs Tracker sheet.",
            0x3498DB,
        )
    )


@bot.event
async def on_ready():
    print(f"[items] logged in as {bot.user}", flush=True)
    if _STATE.officer_channel_id is None:
        # Nothing to restore from until an admin has named the channel.
        # Scan every readable text channel's pins once, so a redeploy
        # recovers without anyone re-running !setofficerchannel.
        for guild in bot.guilds:
            for channel in guild.text_channels:
                try:
                    if await load_state(channel):
                        print(f"[items] restored state from #{channel.name}", flush=True)
                        return
                except discord.HTTPException:
                    continue
        print("[items] no state found; run !setofficerchannel", flush=True)
        return

    channel = bot.get_channel(_STATE.officer_channel_id)
    if channel is not None:
        await load_state(channel)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=error_embed("Not allowed", "That command is for administrators."))
        return
    print(f"[items] command error: {error!r}", flush=True)
    await ctx.send(embed=error_embed("Something went wrong", str(error)))


def main() -> None:
    global _SPREADSHEET
    missing = missing_credentials(os.environ)
    if missing:
        print(
            f"[items] not configured, missing: {', '.join(missing)}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_NOT_CONFIGURED)

    _SPREADSHEET = items_sheet.open_logs_tracker(
        os.environ["ITEMS_SHEET_ID"], os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    )
    bot.run(os.environ["ITEMS_DISCORD_TOKEN"])


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS — all suites

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Add member helper commands and bot startup"
```

---

## Task 12: Supervisor, deployment config and the memory gate

**Files:**
- Modify: `supervisor.py`
- Modify: `render.yaml`
- Modify: `README.md`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `items_bot.py` as a runnable script.
- Produces: a third supervised child named `items`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_supervisor.py


def test_the_item_bot_is_supervised():
    names = [spec.name for spec in supervisor.CHILDREN]
    assert "items" in names


def test_the_item_bot_stays_stopped_when_not_configured():
    """Exit 78 must not crash-loop.

    A crash-looping child on a 0.1 CPU instance competes with the timer
    for the same shared resources, which is exactly what the default
    NO_RESTART_CODES policy exists to prevent.
    """
    spec = next(s for s in supervisor.CHILDREN if s.name == "items")
    assert not supervisor.should_restart(78, spec.no_restart_codes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_supervisor.py -k item -v`
Expected: FAIL with `AssertionError: assert 'items' in ['timer', 'attendance']`

- [ ] **Step 3: Add the child spec**

In `supervisor.py`, append one entry to `CHILDREN`. Do not modify the existing two entries.

```python
CHILDREN = [
    # bot.py exits 0 whenever bot.run() returns normally -- not only when
    # deliberately stopped -- so the timer must always be relaunched,
    # regardless of exit code.
    ChildSpec(
        "timer", [sys.executable, "-u", "bot.py"], no_restart_codes=frozenset()
    ),
    ChildSpec("attendance", [sys.executable, "-u", "attendance_bot.py"]),
    ChildSpec("items", [sys.executable, "-u", "items_bot.py"]),
]
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Add the env vars to render.yaml**

Append to the existing `envVars` list. Change nothing else in the file.

```yaml
      - key: ITEMS_DISCORD_TOKEN
        sync: false
      - key: ITEMS_SHEET_ID
        sync: false
      - key: ITEMS_GEAR_DAILY_CAP
        value: "3"
```

- [ ] **Step 6: Measure memory before shipping**

This is a gate, not a formality: three `discord.py` processes on a 512MB free instance is the main deployment risk in this plan.

```bash
.venv/bin/python -c "
import subprocess, sys, time, os
os.environ.setdefault('ITEMS_DISCORD_TOKEN', 'x')
p = subprocess.Popen([sys.executable, '-c', 'import discord, gspread, items_bot; import time; time.sleep(20)'])
time.sleep(8)
print(subprocess.run(['ps', '-o', 'rss=', '-p', str(p.pid)], capture_output=True, text=True).stdout)
p.terminate()
"
```

Record the RSS in KB. Multiply by three and compare against 512MB, leaving headroom for the timer's HTTP server.

**If the total exceeds roughly 400MB:** stop and report to the user rather than deploying. The documented fallback is running the item commands inside the attendance process (same modules, shared fate). Do not make that call unilaterally.

- [ ] **Step 7: Document the bot in README.md**

Add a section covering: the three commands, the two rules, the `!setofficerchannel` setup step, the env vars, and the Discord application setup (Message Content Intent; permissions View Channels, Send Messages, Embed Links, Read Message History, Manage Messages; share the spreadsheet with the service account as Editor).

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS — including every pre-existing attendance and supervisor test, unchanged

- [ ] **Step 9: Commit**

```bash
git add supervisor.py render.yaml README.md tests/test_supervisor.py
git commit -m "Supervise the item bot as a third process"
```

---

## Task 13: Live verification against the real sheet

**Files:** none — this is a manual verification pass.

Automated tests use fakes, so two things remain unproven: that gspread writes a real Google Sheets **checkbox** correctly, and that the whole path works end to end.

- [ ] **Step 1: Confirm the checkbox write against a scratch copy**

Make a copy of Logs Tracker, share it with the service account, and run:

```bash
.venv/bin/python -c "
import os, items_sheet
from dotenv import load_dotenv; load_dotenv()
ss = items_sheet.open_logs_tracker(os.environ['SCRATCH_SHEET_ID'], os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
print(items_sheet.record_special(ss, '<a test player>', '<a test item>'))
"
```

Open the sheet and confirm the checkbox is **ticked**, not showing the text `TRUE` beside an empty box. If gspread wrote a string instead of a boolean, change the payload in `record_special` to use `value_input_option="USER_ENTERED"` via `worksheet.update(address, [[True]], value_input_option="USER_ENTERED")` and re-verify.

- [ ] **Step 2: Verify the ledger tab is created with the right header**

Confirm a `Distribution Log` tab appeared in the scratch copy with the seven columns in `LEDGER_HEADER` order.

- [ ] **Step 3: End-to-end in Discord**

With the bot running against the scratch sheet:

1. `!setofficerchannel` in the private channel → confirm the pinned state message appears.
2. `!request <special item> <IGN>` from a member account → queued.
3. `!request` the same thing again → refused as already pending.
4. `!distribute` in the officer channel → panel lists it.
5. Approve → checkbox ticks, ledger row appears, panel updates.
6. `!request` that same special again → refused as already held.
7. Four gear requests for one player, approve three → the fourth is refused.
8. `!distribute` in a non-officer channel → no response.
9. Restart the bot process → `!distribute` still shows the pending queue.

- [ ] **Step 4: Point at the real sheet and deploy**

Set `ITEMS_SHEET_ID` to `1Xx44UKBx0v5Pa0xbBzuVElEFZK-mdeQ5jHBBzBsKQgc` in Render, deploy, and confirm from the Render logs that all three processes start and the timer and attendance bots are unaffected.

---

## Notes for the implementer

**Why the cap is checked twice.** Task 4 builds one `check_eligibility`; Task 9 calls it at request time and Task 10 calls it again inside the lock at approval time. Neither call is redundant. Removing the first lets members flood the officers' panel with ineligible requests; removing the second lets a member queue five requests before any approval and receive all five.

**Why writes go through `batch_update`.** It matches `attendance_sheet.apply_writes`, and it lets tests assert on the payload rather than on how `FakeWorksheet` stringifies a value. `update_cell` would work but couples the tests to the fake.

**Why `resolve_item` refuses to fuzzy-match while `resolve_ign` is also exact.** Item names differ by a single word (`Asta's Belt` / `Asta's Heart`), and an approval is a permanent record. Fuzzy matching is right for OCR output in `attendance_roster` — a human typing a command can be asked to try again.

**What must never happen.** No player row created, no item column created, no ledger row without its cell write, and no request silently dropped. Every one of those has a test.
