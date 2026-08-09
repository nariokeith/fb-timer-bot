# Special Log Raffle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move special logs out of `!request` and into a poll-driven raffle run by three new commands (`!poll`, `!list`, `!winner`) in the same item bot, with the `Special Logs` checkbox remaining the record of who already owns what.

**Architecture:** A new pure module `items_raffle.py` holds everything decidable without a network — turning a Discord nickname into an IGN, splitting voters into eligible / already-has / unidentified, and parsing command arguments. `items_state.py` gains a `Raffle` record persisted in the existing pinned-message shards. `items_rules.py` stops returning special items from `resolve_item`. `items_bot.py` gains the commands and the Discord plumbing. Sheet writes reuse `items_sheet.commit_approval` unchanged.

**Tech Stack:** Python 3.13, discord.py 2.7.1 (native `discord.Poll`), gspread 6.2.1, pytest.

## Global Constraints

- Design doc: `docs/superpowers/specs/2026-08-09-special-log-raffle-design.md`. Read it before starting.
- Run tests with `.venv/bin/python -m pytest` from the repo root.
- No new dependencies. No new Discord token, application, or spreadsheet.
- Timestamps are PHT strings in `items_rules.TIMESTAMP_FORMAT` (`%Y-%m-%d %H:%M:%S`). Compare them as strings; the format sorts lexicographically.
- Never fuzzy-match a name into a permanent sheet write. Exact normalized matching plus `attendance_roster.ALIASES` only. Fuzzy matching is allowed *only* to produce "did you mean" suggestions in refusals.
- Refuse rather than guess: an ambiguous reading is an error message, never a choice.
- `MAX_RAFFLES = 5`, `DEFAULT_POLL_HOURS = 24`, `MIN_POLL_HOURS = 1`, `MAX_POLL_HOURS = 168`.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Work on a branch, not `main`.

---

### Task 1: Nickname to IGN resolution

Members' Discord nicknames carry a guild tag: `Jjew`, `BK | Jjew`, `M2 | Jjew`, `BK Jjew`, `BK - Jjew`. The roster in the sheet holds the bare IGN. This task turns one into the other.

**Files:**
- Create: `items_raffle.py`
- Test: `tests/test_items_raffle.py`

**Interfaces:**
- Consumes: `items_rules.resolve_ign(query, roster) -> str | None` (exact normalized match plus `ALIASES`; raises `items_rules.RequestParseError` when two roster rows normalize identically).
- Produces:
  - `items_raffle.SEPARATORS: str`
  - `items_raffle.nickname_candidates(nickname: str) -> list[str]`
  - `items_raffle.resolve_voter(nickname: str, roster: list[str]) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_items_raffle.py`:

```python
"""Tests for the raffle's pure logic.

No Discord, no Google Sheets, no clock. Everything here is decided from
values passed in, which is what makes the nickname rules testable.
"""

import pytest

import items_raffle

ROSTER = ["Jjew", "Kobe", "Ryuu", "chinchong ni Mumu", "wile-KAMOTE"]


@pytest.mark.parametrize(
    "nickname",
    ["Jjew", "BK | Jjew", "M2 | Jjew", "BK Jjew", "BK - Jjew", "  BK|Jjew  "],
)
def test_every_tag_format_resolves_to_the_bare_ign(nickname):
    assert items_raffle.resolve_voter(nickname, ROSTER) == "Jjew"


def test_a_multi_word_ign_survives_tag_stripping():
    assert (
        items_raffle.resolve_voter("M2 - chinchong ni Mumu", ROSTER)
        == "chinchong ni Mumu"
    )


def test_an_ign_containing_a_hyphen_is_not_split_apart():
    """The remainder is a slice of the original string, not re-joined tokens.

    Re-joining would have to reconstruct 'wile-KAMOTE' from ['wile',
    'KAMOTE'] and would silently produce 'wile KAMOTE', which is not a
    roster row.
    """
    assert items_raffle.resolve_voter("BK | wile-KAMOTE", ROSTER) == "wile-KAMOTE"


def test_an_alias_resolves_through_the_roster():
    assert items_raffle.resolve_voter("BK | KobePH", ROSTER) == "Kobe"


def test_a_tag_that_is_itself_a_roster_name_is_still_only_a_tag():
    """Candidates are suffixes, so the leading 'Kobe' can never win.

    The guild tag is always on the left. A member called Jjew whose tag
    happens to be another player's name still resolves to Jjew.
    """
    assert items_raffle.resolve_voter("Kobe | Jjew", ROSTER) == "Jjew"


def test_a_nickname_matching_two_different_rows_does_not_resolve():
    """Two roster rows where one is a suffix of the other. Refuse, don't pick.

    'BK | chinchong ni Mumu' produces both 'chinchong ni Mumu' and
    'ni Mumu' as candidates. If the sheet ever holds both, the bot has
    no way to tell which player is meant.
    """
    roster = ROSTER + ["ni Mumu"]

    assert items_raffle.resolve_voter("BK | chinchong ni Mumu", roster) is None


def test_an_unknown_nickname_does_not_resolve():
    assert items_raffle.resolve_voter("BK | Nobody", ROSTER) is None


def test_an_empty_nickname_does_not_resolve():
    assert items_raffle.resolve_voter("   ", ROSTER) is None


def test_candidates_include_the_whole_nickname_first():
    assert items_raffle.nickname_candidates("BK | Jjew")[0] == "BK | Jjew"


def test_candidates_do_not_repeat():
    candidates = items_raffle.nickname_candidates("BK  |  Jjew")
    assert len(candidates) == len(set(candidates))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_raffle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'items_raffle'`

- [ ] **Step 3: Write the implementation**

Create `items_raffle.py`:

```python
"""Pure logic for the special log raffle.

No Discord, no Google Sheets, no clock. The bot passes in the voters it
fetched and the sheet it read; everything decided here is decided from
those values, which is what makes the nickname rules testable without a
network.

The hard part is identity. Discord gives the bot a nickname; the sheet
is keyed by IGN. Nicknames carry a guild tag ("BK | Jjew") in several
formats, and the roster contains multi-word rows ("chinchong ni Mumu")
and hyphenated rows ("wile-KAMOTE"), so neither "take the last word" nor
"split on separators and re-join" can work.
"""

from attendance_roster import normalize
import items_rules

# Characters that can sit between a guild tag and the IGN. Whitespace is
# handled separately because it is also a legitimate part of an IGN.
SEPARATORS = "|-:/"


def nickname_candidates(nickname: str) -> list[str]:
    """Every substring of the nickname that might be the IGN.

    The whole nickname first, then the remainder after each separator.
    Each remainder is a SLICE of the original string rather than a
    re-join of split tokens: re-joining would have to reconstruct the
    internal spacing of 'chinchong ni Mumu' and would corrupt any IGN
    containing one of the separator characters.
    """
    text = " ".join(nickname.split())
    if not text:
        return []

    candidates = [text]
    for index, character in enumerate(text):
        if character not in SEPARATORS and not character.isspace():
            continue
        remainder = text[index + 1 :].lstrip(SEPARATORS + " \t")
        if remainder and remainder not in candidates:
            candidates.append(remainder)
    return candidates


def resolve_voter(nickname: str, roster: list[str]) -> str | None:
    """The roster row this nickname belongs to, or None.

    None when nothing matches AND when two candidates match two
    different rows. An ambiguous nickname is reported to an officer
    rather than guessed: the consequence of a wrong answer here is a
    permanently ticked checkbox on the wrong player.
    """
    matched: list[str] = []
    for candidate in nickname_candidates(nickname):
        try:
            player = items_rules.resolve_ign(candidate, roster)
        except items_rules.RequestParseError:
            # Two roster rows normalise identically. That is a sheet
            # problem, not this voter's; nobody can be resolved safely.
            return None
        if player is not None and player not in matched:
            matched.append(player)

    return matched[0] if len(matched) == 1 else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_raffle.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add items_raffle.py tests/test_items_raffle.py
git commit -m "Resolve a Discord nickname to a roster IGN

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Splitting voters into eligible, already-has, and unidentified

**Files:**
- Modify: `items_raffle.py` (append)
- Test: `tests/test_items_raffle.py` (append)

**Interfaces:**
- Consumes: `items_raffle.resolve_voter` from Task 1.
- Produces:
  - `items_raffle.Voter` — frozen dataclass, fields `user_id: int`, `display_name: str`
  - `items_raffle.VoterSplit` — frozen dataclass, fields `eligible: list[str]`, `already_have: list[str]`, `unidentified: list[Voter]`
  - `items_raffle.classify_voters(voters: list[Voter], roster: list[str], holds: Callable[[str], bool]) -> VoterSplit`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_raffle.py`:

```python
def _voter(user_id, display_name):
    return items_raffle.Voter(user_id=user_id, display_name=display_name)


def test_voters_split_into_eligible_already_have_and_unidentified():
    voters = [
        _voter(1, "BK | Jjew"),
        _voter(2, "M2 - Kobe"),
        _voter(3, "BK | Nobody"),
    ]
    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: ign == "Kobe"
    )

    assert split.eligible == ["Jjew"]
    assert split.already_have == ["Kobe"]
    assert [v.user_id for v in split.unidentified] == [3]


def test_the_same_player_voting_from_two_accounts_is_listed_once():
    voters = [_voter(1, "BK | Jjew"), _voter(2, "Jjew")]
    split = items_raffle.classify_voters(voters, ROSTER, holds=lambda ign: False)

    assert split.eligible == ["Jjew"]


def test_eligibility_keeps_the_order_players_voted_in():
    voters = [_voter(1, "Ryuu"), _voter(2, "BK | Jjew"), _voter(3, "Kobe")]
    split = items_raffle.classify_voters(voters, ROSTER, holds=lambda ign: False)

    assert split.eligible == ["Ryuu", "Jjew", "Kobe"]


def test_no_voters_gives_three_empty_groups():
    split = items_raffle.classify_voters([], ROSTER, holds=lambda ign: False)

    assert split.eligible == []
    assert split.already_have == []
    assert split.unidentified == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_raffle.py -k classify -v`
Expected: FAIL — `AttributeError: module 'items_raffle' has no attribute 'Voter'`

- [ ] **Step 3: Write the implementation**

Append to `items_raffle.py`:

```python
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Voter:
    """One Discord account that answered the poll.

    display_name is the server nickname when the bot could see the
    member, and the account's global name otherwise. user_id is carried
    so an unidentified voter can be named by mention in the reply --
    a nickname alone would not let an officer find them.
    """

    user_id: int
    display_name: str


@dataclass(frozen=True)
class VoterSplit:
    eligible: list[str] = field(default_factory=list)
    already_have: list[str] = field(default_factory=list)
    unidentified: list[Voter] = field(default_factory=list)


def classify_voters(
    voters: list[Voter],
    roster: list[str],
    holds: Callable[[str], bool],
) -> VoterSplit:
    """Split the poll's voters into the three groups an officer needs.

    `holds` answers "is this player's checkbox already ticked for this
    special log". It is passed in rather than read here so this stays
    pure: the bot binds it to the snapshot it already read.

    Duplicates are collapsed by resolved IGN, so a member voting from an
    alt account cannot appear in the pool twice and double their odds.
    Order is the order they voted in -- stable, and visibly not shuffled
    by the bot, since the draw itself is done by a human.
    """
    eligible: list[str] = []
    already_have: list[str] = []
    unidentified: list[Voter] = []
    seen: set[str] = set()

    for voter in voters:
        player = resolve_voter(voter.display_name, roster)
        if player is None:
            unidentified.append(voter)
            continue
        key = normalize(player)
        if key in seen:
            continue
        seen.add(key)
        (already_have if holds(player) else eligible).append(player)

    return VoterSplit(
        eligible=eligible, already_have=already_have, unidentified=unidentified
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_raffle.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add items_raffle.py tests/test_items_raffle.py
git commit -m "Split poll voters into eligible, already-has, and unidentified

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Command argument parsing

`!poll Asta's Heart --hours 48` and `!winner Asta's Heart chinchong ni Mumu` both have a multi-word item name, so neither can be split by position.

**Files:**
- Modify: `items_raffle.py` (append)
- Test: `tests/test_items_raffle.py` (append)

**Interfaces:**
- Consumes: `items_rules.resolve_ign` from Task 1's imports.
- Produces:
  - `items_raffle.DEFAULT_POLL_HOURS = 24`, `MIN_POLL_HOURS = 1`, `MAX_POLL_HOURS = 168`
  - `items_raffle.HOURS_FLAG = "--hours"`
  - `items_raffle.RaffleArgumentError(RuntimeError)`
  - `items_raffle.PollArgument` — frozen dataclass, fields `item_query: str`, `hours: int`
  - `items_raffle.parse_poll_argument(argument: str) -> PollArgument`
  - `items_raffle.split_item_and_ign(argument: str, item_names: list[str], roster: list[str]) -> tuple[str, str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_raffle.py`:

```python
def test_a_poll_argument_without_a_flag_uses_the_default_duration():
    parsed = items_raffle.parse_poll_argument("Asta's Heart")

    assert parsed.item_query == "Asta's Heart"
    assert parsed.hours == items_raffle.DEFAULT_POLL_HOURS


def test_the_hours_flag_overrides_the_duration_and_is_stripped():
    parsed = items_raffle.parse_poll_argument("Asta's Heart --hours 48")

    assert parsed.item_query == "Asta's Heart"
    assert parsed.hours == 48


def test_an_hours_flag_without_a_number_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="--hours"):
        items_raffle.parse_poll_argument("Asta's Heart --hours")


def test_a_non_numeric_hours_value_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="whole number"):
        items_raffle.parse_poll_argument("Asta's Heart --hours banana")


@pytest.mark.parametrize("hours", [0, 169])
def test_an_out_of_range_duration_is_refused(hours):
    with pytest.raises(items_raffle.RaffleArgumentError, match="between 1 and 168"):
        items_raffle.parse_poll_argument(f"Asta's Heart --hours {hours}")


def test_an_empty_poll_argument_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Usage"):
        items_raffle.parse_poll_argument("   ")


def test_a_poll_argument_that_is_only_a_flag_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Usage"):
        items_raffle.parse_poll_argument("--hours 48")


def test_winner_splits_a_multi_word_item_from_a_multi_word_ign():
    item, ign = items_raffle.split_item_and_ign(
        "Asta's Heart chinchong ni Mumu", ["Asta's Heart"], ROSTER
    )

    assert (item, ign) == ("Asta's Heart", "chinchong ni Mumu")


def test_winner_resolves_the_ign_through_an_alias():
    item, ign = items_raffle.split_item_and_ign(
        "Asta's Heart KobePH", ["Asta's Heart"], ROSTER
    )

    assert (item, ign) == ("Asta's Heart", "Kobe")


def test_winner_refuses_an_unknown_item():
    with pytest.raises(items_raffle.RaffleArgumentError, match="No open raffle"):
        items_raffle.split_item_and_ign("Benji's Heart Jjew", ["Asta's Heart"], ROSTER)


def test_winner_refuses_an_unknown_player():
    with pytest.raises(items_raffle.RaffleArgumentError, match="No player named"):
        items_raffle.split_item_and_ign(
            "Asta's Heart Nobody", ["Asta's Heart"], ROSTER
        )


def test_winner_refuses_a_single_word_argument():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Usage"):
        items_raffle.split_item_and_ign("Asta's", ["Asta's Heart"], ROSTER)


def test_winner_refuses_an_argument_that_reads_two_ways():
    """'Kobe' is both a raffle name and a player here. Refuse, don't pick."""
    with pytest.raises(items_raffle.RaffleArgumentError, match="more than one way"):
        items_raffle.split_item_and_ign("Kobe Kobe Jjew", ["Kobe", "Kobe Kobe"], ROSTER + ["Kobe Jjew"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_raffle.py -k "poll_argument or winner" -v`
Expected: FAIL — `AttributeError: module 'items_raffle' has no attribute 'parse_poll_argument'`

- [ ] **Step 3: Write the implementation**

Append to `items_raffle.py`:

```python
from difflib import get_close_matches

DEFAULT_POLL_HOURS = 24
MIN_POLL_HOURS = 1
# Discord allows longer, but a week is already far past any real raffle
# and a bounded value can never be rejected by the API mid-command.
MAX_POLL_HOURS = 168

HOURS_FLAG = "--hours"

POLL_USAGE = "Usage: `!poll <special log name> [--hours N]`"
WINNER_USAGE = "Usage: `!winner <special log name> <IGN>`"


class RaffleArgumentError(RuntimeError):
    """A raffle command's argument does not resolve. Message is user-facing."""


@dataclass(frozen=True)
class PollArgument:
    item_query: str
    hours: int


def parse_poll_argument(argument: str) -> PollArgument:
    """Split '<item name> [--hours N]'.

    The flag is trailing because the item name is multi-word and
    unquoted, so a leading flag would be indistinguishable from the
    first word of a name. '--hours' cannot occur inside a sheet header.
    """
    words = argument.split()
    hours = DEFAULT_POLL_HOURS

    if HOURS_FLAG in words:
        index = words.index(HOURS_FLAG)
        value = words[index + 1 :]
        if len(value) != 1:
            raise RaffleArgumentError(
                f"`{HOURS_FLAG}` takes exactly one number and must come last. "
                f"{POLL_USAGE}"
            )
        try:
            hours = int(value[0])
        except ValueError:
            raise RaffleArgumentError(
                f"`{HOURS_FLAG} {value[0]}` is not a whole number of hours."
            ) from None
        if not MIN_POLL_HOURS <= hours <= MAX_POLL_HOURS:
            raise RaffleArgumentError(
                f"A poll must run between {MIN_POLL_HOURS} and "
                f"{MAX_POLL_HOURS} hours."
            )
        words = words[:index]

    item_query = " ".join(words)
    if not item_query:
        raise RaffleArgumentError(f"Which special log? {POLL_USAGE}")
    return PollArgument(item_query=item_query, hours=hours)


def split_item_and_ign(
    argument: str, item_names: list[str], roster: list[str]
) -> tuple[str, str]:
    """Split '<item name> <IGN>' where BOTH parts may contain spaces.

    Every split point is tried; a reading is accepted only when the
    prefix names one of `item_names` and the suffix resolves to a roster
    row. Two valid readings is a refusal, not a coin toss -- the write
    this feeds is a permanent checkbox.

    Ascending split index tries the LONGEST IGN first, which is what
    lets 'chinchong ni Mumu' win over a shorter player name inside it.
    """
    words = argument.split()
    if len(words) < 2:
        raise RaffleArgumentError(WINNER_USAGE)

    index = {normalize(name): name for name in item_names}
    readings: list[tuple[str, str]] = []
    saw_known_ign = False

    for i in range(1, len(words)):
        candidate_ign = " ".join(words[i:])
        try:
            player = items_rules.resolve_ign(candidate_ign, roster)
        except items_rules.RequestParseError as exc:
            raise RaffleArgumentError(str(exc)) from None
        if player is None:
            continue
        saw_known_ign = True
        item = index.get(normalize(" ".join(words[:i])))
        if item is not None:
            readings.append((item, player))

    if len(readings) == 1:
        return readings[0]
    if len(readings) > 1:
        spelled = "; ".join(f"{item!r} for {ign!r}" for item, ign in readings)
        raise RaffleArgumentError(
            f"That could be read more than one way ({spelled}). Refusing to guess."
        )
    if saw_known_ign:
        suggestions = get_close_matches(argument, item_names, n=3, cutoff=0.5)
        hint = f" Open raffles: {', '.join(suggestions)}." if suggestions else ""
        raise RaffleArgumentError(
            f"No open raffle matches that name.{hint} {WINNER_USAGE}"
        )

    tail = words[-1]
    suggestions = get_close_matches(tail, roster, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise RaffleArgumentError(
        f"No player named {tail!r} in the sheet.{hint} The IGN goes last: "
        f"{WINNER_USAGE}"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_raffle.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add items_raffle.py tests/test_items_raffle.py
git commit -m "Parse the raffle commands' multi-word arguments

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Persisting raffles in the pinned state

**Files:**
- Modify: `items_state.py`
- Test: `tests/test_items_state.py` (append)

**Interfaces:**
- Consumes: `items_state.State`, `items_state.encode_state`, `items_state.decode_state`, `items_state.decode_shards`, `items_state.MAX_CONTENT`, `items_state.MAX_SHARDS` (all existing).
- Produces:
  - `items_state.MAX_RAFFLES = 5`
  - `items_state.Raffle` — frozen dataclass: `item: str`, `channel_id: int`, `message_id: int`, `created_at: str`, `ends_at: str`, `eligible: tuple[str, ...] = ()`, `listed: bool = False`, `winner: str = ""`; methods `to_dict()`, `from_dict(raw)`
  - `State.raffle_role_ids: list[int]`, `State.raffle_channel_id: int | None`, `State.raffles: list[Raffle]`
  - `items_state.find_raffle(state, item) -> Raffle | None`
  - `items_state.replace_raffle(state, raffle, **changes) -> Raffle`
  - `items_state.raffle_item_names(state) -> list[str]`
  - `items_state.evict_for_new_raffle(state, now: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_state.py`:

```python
def _raffle(item="Asta's Heart", created="2026-08-09 10:00:00", ends="2026-08-10 10:00:00", **kwargs):
    return items_state.Raffle(
        item=item,
        channel_id=555,
        message_id=777,
        created_at=created,
        ends_at=ends,
        **kwargs,
    )


def test_a_raffle_survives_an_encode_decode_round_trip():
    state = items_state.State(
        officer_channel_id=1,
        raffle_channel_id=2,
        raffle_role_ids=[10, 11],
        raffles=[_raffle(eligible=("Jjew", "Kobe"), listed=True, winner="Jjew")],
    )

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.raffle_channel_id == 2
    assert restored.raffle_role_ids == [10, 11]
    assert len(restored.raffles) == 1
    assert restored.raffles[0].item == "Asta's Heart"
    assert restored.raffles[0].eligible == ("Jjew", "Kobe")
    assert restored.raffles[0].listed is True
    assert restored.raffles[0].winner == "Jjew"


def test_a_pin_written_before_raffles_existed_still_loads():
    """Production pins have none of the three new keys."""
    old = items_state.State(officer_channel_id=1)
    contents = items_state.encode_state(old)

    restored = items_state.decode_shards(contents)

    assert restored.raffles == []
    assert restored.raffle_role_ids == []
    assert restored.raffle_channel_id is None


def test_raffles_spill_into_further_shards_rather_than_being_dropped():
    state = items_state.State(
        officer_channel_id=1,
        raffles=[
            _raffle(item=f"Special Log {n}", eligible=tuple(f"Player {i:03d}" for i in range(40)))
            for n in range(items_state.MAX_RAFFLES)
        ],
    )

    contents = items_state.encode_state(state)
    restored = items_state.decode_shards(contents)

    assert len(contents) > 1
    assert [r.item for r in restored.raffles] == [r.item for r in state.raffles]


def test_find_raffle_matches_case_and_spacing_insensitively():
    state = items_state.State(raffles=[_raffle()])

    assert items_state.find_raffle(state, "  asta's   heart ").item == "Asta's Heart"
    assert items_state.find_raffle(state, "Benji's Heart") is None


def test_find_raffle_returns_the_most_recent_when_a_name_repeats():
    state = items_state.State(
        raffles=[
            _raffle(created="2026-08-01 10:00:00", winner="Kobe"),
            _raffle(created="2026-08-09 10:00:00"),
        ]
    )

    assert items_state.find_raffle(state, "Asta's Heart").created_at == "2026-08-09 10:00:00"


def test_replace_raffle_swaps_the_record_in_place():
    original = _raffle()
    state = items_state.State(raffles=[original])

    updated = items_state.replace_raffle(state, original, winner="Jjew")

    assert state.raffles == [updated]
    assert updated.winner == "Jjew"
    assert original.winner == ""


def test_evicting_drops_the_oldest_ended_raffle_when_full():
    state = items_state.State(
        raffles=[
            _raffle(item=f"Log {n}", created=f"2026-08-0{n + 1} 10:00:00", ends=f"2026-08-0{n + 1} 12:00:00")
            for n in range(items_state.MAX_RAFFLES)
        ]
    )

    assert items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert [r.item for r in state.raffles] == [f"Log {n}" for n in range(1, items_state.MAX_RAFFLES)]


def test_evicting_refuses_when_every_raffle_is_still_open():
    state = items_state.State(
        raffles=[
            _raffle(item=f"Log {n}", ends="2026-12-31 23:59:59")
            for n in range(items_state.MAX_RAFFLES)
        ]
    )

    assert not items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert len(state.raffles) == items_state.MAX_RAFFLES


def test_evicting_does_nothing_below_the_ceiling():
    state = items_state.State(raffles=[_raffle()])

    assert items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert len(state.raffles) == 1


def test_raffle_item_names_lists_every_tracked_raffle():
    state = items_state.State(raffles=[_raffle(item="A"), _raffle(item="B")])

    assert items_state.raffle_item_names(state) == ["A", "B"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_state.py -k raffle -v`
Expected: FAIL — `AttributeError: module 'items_state' has no attribute 'Raffle'`

- [ ] **Step 3: Write the implementation**

In `items_state.py`, add `import dataclasses` beside the existing
`from dataclasses import dataclass, field` line (`replace_raffle` needs
`dataclasses.replace`), then add after the `PendingRequest` class:

```python
# The pinned messages are capped at MAX_SHARDS, and a listed raffle
# carries an eligible IGN for every voter. Five is enough history to
# re-read recent draws without crowding the queue out of the pins.
MAX_RAFFLES = 5


@dataclass(frozen=True)
class Raffle:
    """One special log poll and everything decided from it.

    `listed` cannot be inferred from `eligible`: a raffle where nobody
    was eligible is a real outcome, and it must stay distinguishable
    from one that has not been listed yet -- otherwise !winner would
    tell an officer to run !list again forever.
    """

    item: str
    channel_id: int
    message_id: int
    created_at: str
    ends_at: str
    eligible: tuple[str, ...] = ()
    listed: bool = False
    winner: str = ""

    def to_dict(self) -> dict:
        return {
            "item": self.item,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "created_at": self.created_at,
            "ends_at": self.ends_at,
            "eligible": list(self.eligible),
            "listed": self.listed,
            "winner": self.winner,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Raffle":
        return cls(
            item=str(raw["item"]),
            channel_id=int(raw["channel_id"]),
            message_id=int(raw["message_id"]),
            created_at=str(raw["created_at"]),
            ends_at=str(raw["ends_at"]),
            eligible=tuple(str(name) for name in raw.get("eligible", [])),
            listed=bool(raw.get("listed", False)),
            winner=str(raw.get("winner", "")),
        )
```

Add the three fields to `State`, after `igns`:

```python
    # Roles permitted to run !poll / !list / !winner, and the one channel
    # they work in. Unlike !distribute -- which is authorised by the
    # officer channel itself -- the raffle happens in a member-visible
    # channel, so the channel cannot also be the permission.
    raffle_role_ids: list[int] = field(default_factory=list)
    raffle_channel_id: int | None = None
    raffles: list["Raffle"] = field(default_factory=list)
```

In `_encode_with_total`, add to `first_payload` construction (after the `board_message_id` block):

```python
    if state.raffle_channel_id is not None:
        first_payload["raffle_channel_id"] = state.raffle_channel_id
    if state.raffle_role_ids:
        first_payload["raffle_role_ids"] = list(state.raffle_role_ids)
    first_payload["raffles"] = []
```

and in the same function, immediately before `return [_render(payload) for payload in payloads]`, add the raffle spill loop (mirroring the queue loop above it):

```python
    for raffle in state.raffles:
        raffle_payload = raffle.to_dict()
        current = payloads[-1]
        current.setdefault("raffles", []).append(raffle_payload)
        if len(_render(current)) <= MAX_CONTENT:
            continue

        current["raffles"].pop()
        current = {"part": len(payloads), "total": total, "raffles": []}
        payloads.append(current)

        current["raffles"].append(raffle_payload)
        if len(_render(current)) > MAX_CONTENT:
            raise ValueError("a raffle is too large for a state shard")
```

In `decode_state`, inside the `try`, after the `igns` line:

```python
        raffles = [Raffle.from_dict(r) for r in payload.get("raffles", [])]
        raffle_channel_id = payload.get("raffle_channel_id")
        raffle_channel_id = (
            int(raffle_channel_id) if raffle_channel_id is not None else None
        )
        raffle_role_ids = [int(r) for r in payload.get("raffle_role_ids", [])]
```

and pass them into the returned `State(...)`:

```python
            raffle_role_ids=raffle_role_ids,
            raffle_channel_id=raffle_channel_id,
            raffles=raffles,
```

In `decode_shards`, after the `board_message_id` block:

```python
    raffle_channel_id = next(
        (
            shard.state.raffle_channel_id
            for shard in shards
            if shard.state.raffle_channel_id is not None
        ),
        None,
    )
    raffle_role_ids = next(
        (
            shard.state.raffle_role_ids
            for shard in shards
            if shard.state.raffle_role_ids
        ),
        [],
    )
```

Add `raffles: list[Raffle] = []` beside the existing `queue: list[PendingRequest] = []`, extend it in the shard loop:

```python
        raffles.extend(shard.state.raffles)
```

and pass all three into the returned `State(...)`:

```python
        raffle_role_ids=raffle_role_ids,
        raffle_channel_id=raffle_channel_id,
        raffles=raffles,
```

Append the helpers at the end of `items_state.py`:

```python
def find_raffle(state: State, item: str) -> Raffle | None:
    """The most recent raffle for this special log, or None.

    Most recent rather than first, because a log raffled twice (a second
    copy dropped later) leaves an older closed record in state; the
    officer always means the live one.
    """
    wanted = items_rules.normalize(item)
    matches = [r for r in state.raffles if items_rules.normalize(r.item) == wanted]
    if not matches:
        return None
    return max(matches, key=lambda raffle: raffle.created_at)


def replace_raffle(state: State, raffle: Raffle, **changes) -> Raffle:
    """Swap a raffle for an updated copy, in place. Returns the new one."""
    updated = dataclasses.replace(raffle, **changes)
    state.raffles[state.raffles.index(raffle)] = updated
    return updated


def raffle_item_names(state: State) -> list[str]:
    return [raffle.item for raffle in state.raffles]


def evict_for_new_raffle(state: State, now: str) -> bool:
    """Make room for one more raffle. False when there is none to make.

    Only a raffle whose poll has already closed may be dropped. Evicting
    a live one would orphan a poll members are still voting in, with no
    way to draw from it.
    """
    if len(state.raffles) < MAX_RAFFLES:
        return True
    ended = [raffle for raffle in state.raffles if raffle.ends_at <= now]
    if not ended:
        return False
    state.raffles.remove(min(ended, key=lambda raffle: raffle.created_at))
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_state.py -v`
Expected: PASS — all new tests plus every existing one.

- [ ] **Step 5: Commit**

```bash
git add items_state.py tests/test_items_state.py
git commit -m "Persist raffles alongside the request queue

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `!request` refuses special logs

**Files:**
- Modify: `items_rules.py:137-169` (`resolve_item`), plus a new `resolve_special`
- Test: `tests/test_items_rules.py`

**Interfaces:**
- Produces:
  - `items_rules.resolve_item` — unchanged signature, but now raises `ItemLookupError` when the query names a Special Logs column.
  - `items_rules.resolve_special(query: str, special_headers: list[str], gear_headers: list[str]) -> str` — returns the canonical special log header, raises `ItemLookupError` otherwise.
  - `items_rules.RAFFLE_REDIRECT: str`, `items_rules.REQUEST_REDIRECT: str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_rules.py`:

```python
SPECIAL_HEADERS = ["Player Name", "Asta's Heart", "Benji's Heart"]
GEAR_HEADERS = ["Player Name", "Dark Orb Earrings", "Sacred Ring"]


def test_requesting_a_special_log_is_refused_with_a_pointer_to_the_raffle():
    with pytest.raises(items_rules.ItemLookupError, match="raffled"):
        items_rules.resolve_item("Asta's Heart", SPECIAL_HEADERS, GEAR_HEADERS)


def test_requesting_a_gear_log_still_resolves():
    item = items_rules.resolve_item("Sacred Ring", SPECIAL_HEADERS, GEAR_HEADERS)

    assert item.name == "Sacred Ring"
    assert item.type == items_rules.GEAR


def test_parse_request_refuses_a_special_log_naming_the_raffle():
    with pytest.raises(items_rules.RequestParseError, match="raffled"):
        items_rules.parse_request(
            "Asta's Heart Kobe", ["Kobe"], SPECIAL_HEADERS, GEAR_HEADERS
        )


def test_resolve_special_returns_the_canonical_header():
    assert (
        items_rules.resolve_special("  asta's   HEART ", SPECIAL_HEADERS, GEAR_HEADERS)
        == "Asta's Heart"
    )


def test_resolve_special_refuses_a_gear_log_with_a_pointer_to_request():
    with pytest.raises(items_rules.ItemLookupError, match="!request"):
        items_rules.resolve_special("Sacred Ring", SPECIAL_HEADERS, GEAR_HEADERS)


def test_resolve_special_suggests_close_names():
    with pytest.raises(items_rules.ItemLookupError, match="Did you mean"):
        items_rules.resolve_special("Asta", SPECIAL_HEADERS, GEAR_HEADERS)


def test_resolve_special_refuses_an_empty_query():
    with pytest.raises(items_rules.ItemLookupError, match="No item name"):
        items_rules.resolve_special("   ", SPECIAL_HEADERS, GEAR_HEADERS)
```

Then search `tests/test_items_rules.py` for existing tests that assert `resolve_item` or `parse_request` returns a `SPECIAL` item, and update them: they now assert the refusal instead. Run `grep -n "SPECIAL" tests/test_items_rules.py` to find them. Do not delete coverage — convert each one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -v`
Expected: FAIL — the new tests error with `AttributeError: ... 'resolve_special'`, and `test_requesting_a_special_log_is_refused...` fails because no exception is raised.

- [ ] **Step 3: Write the implementation**

In `items_rules.py`, add above `resolve_item`:

```python
RAFFLE_REDIRECT = (
    "Special logs are raffled, not requested: watch the raffle channel for "
    "a `!poll` and answer it there."
)
REQUEST_REDIRECT = "Gear logs are requested with `!request`, not raffled."
```

Replace the `if in_special:` branch of `resolve_item` with:

```python
    if in_special:
        raise ItemLookupError(f"{in_special!r} is a special log. {RAFFLE_REDIRECT}")
```

and update the docstring to say that a special log is refused here rather than returned. Keeping the special headers in the lookup is deliberate: they are what make this message possible. Without them the member would only be told "no item column named that", which does not tell them where to go.

Add after `resolve_item`:

```python
def resolve_special(
    query: str, special_headers: list[str], gear_headers: list[str]
) -> str:
    """The Special Logs header this query names, exactly.

    The raffle's counterpart to resolve_item, and exact for the same
    reason: item names differ by one word, and the write this feeds
    ticks a checkbox that can never be untidied by the bot.
    """
    if not query.strip():
        raise ItemLookupError("No item name given.")

    specials = item_names(special_headers)
    gears = item_names(gear_headers)

    found = _exact(query, specials)
    if found:
        return found

    in_gear = _exact(query, gears)
    if in_gear:
        raise ItemLookupError(f"{in_gear!r} is a gear log. {REQUEST_REDIRECT}")

    suggestions = _suggest(query, specials)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ItemLookupError(f"No special log column named {query!r}.{hint}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_rules.py -v`
Expected: PASS

Then run the whole suite to find every other place that assumed a special could be requested:

Run: `.venv/bin/python -m pytest -v`
Expected: failures only in `tests/test_items_bot.py` tests that queue a `Special` request through `!request`. Update those to use a gear item; leave tests that construct a `PendingRequest(type="Special")` directly alone — the approve path still handles them and Task 6 covers the drop.

- [ ] **Step 5: Commit**

```bash
git add items_rules.py tests/test_items_rules.py tests/test_items_bot.py
git commit -m "Refuse special logs in !request and point at the raffle

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Members intent and dropping stranded special requests

**Files:**
- Modify: `items_bot.py:1-12` (docstring), `items_bot.py:53-56` (intents), `items_bot.py:1022-1044` (`on_ready`)
- Test: `tests/test_items_bot.py` (append)

**Interfaces:**
- Produces: `items_bot.drop_special_requests(state) -> list[items_state.PendingRequest]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_bot.py`:

```python
def test_the_members_intent_is_enabled():
    """Poll voters resolve to Members only when this intent is on.

    Without it discord.py falls back to User objects, whose display_name
    is the global name -- not the 'BK | Jjew' server nickname the roster
    match depends on.
    """
    assert items_bot.intents.members is True


def test_dropping_special_requests_removes_only_the_specials():
    state = items_state.State(
        queue=[
            items_state.PendingRequest("a", 1, "Kobe", "Asta's Heart", "Special", "2026-08-09 09:00:00"),
            items_state.PendingRequest("b", 2, "Jjew", "Sacred Ring", "Gear", "2026-08-09 09:01:00"),
        ]
    )

    dropped = items_bot.drop_special_requests(state)

    assert [r.id for r in dropped] == ["a"]
    assert [r.id for r in state.queue] == ["b"]


def test_dropping_nothing_leaves_the_queue_alone():
    state = items_state.State(
        queue=[items_state.PendingRequest("b", 2, "Jjew", "Sacred Ring", "Gear", "2026-08-09 09:01:00")]
    )

    assert items_bot.drop_special_requests(state) == []
    assert [r.id for r in state.queue] == ["b"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -k "members_intent or dropping" -v`
Expected: FAIL — `assert False is True`, then `AttributeError: ... 'drop_special_requests'`

- [ ] **Step 3: Write the implementation**

In `items_bot.py`, change the intents block:

```python
intents = discord.Intents.default()
intents.message_content = True
# Poll voters arrive as Members only when this intent is on; otherwise
# discord.py yields Users, whose display_name is the global name rather
# than the 'BK | Jjew' server nickname the roster match reads. It is a
# privileged intent and must also be enabled for this application in the
# Discord Developer Portal.
intents.members = True
```

Update the module docstring's authorization paragraph to:

```
Authorization has two shapes. !distribute is accepted only in the private
officer channel, so a button there can only be pressed by someone Discord
already lets see the channel. The raffle commands cannot work that way --
the poll must be visible to members -- so they are gated on configured
roles instead.
```

Add near the other queue helpers:

```python
def drop_special_requests(state: items_state.State) -> list[items_state.PendingRequest]:
    """Remove every queued special log request, returning them.

    Special logs are raffled now. A request queued under the old rules
    can no longer be approved into a sensible outcome, and leaving it in
    the queue would show members a board line that never resolves.
    """
    dropped = [r for r in state.queue if r.type == items_rules.SPECIAL]
    for request in dropped:
        state.queue.remove(request)
    return dropped
```

In `on_ready`, after `load_state` succeeds and before the ready log line, add:

```python
        dropped = drop_special_requests(_STATE)
        if dropped:
            await save_state(channel)
            await refresh_board()
            lines = "\n".join(f"• **{r.item}** for **{r.ign}** (<@{r.user_id}>)" for r in dropped)
            await channel.send(
                embed=error_embed(
                    "Special log requests removed",
                    "Special logs are raffled now, so these queued requests "
                    f"were dropped:\n{lines}\n\nTell these members to answer "
                    "the poll in the raffle channel instead.",
                )
            )
```

Read the surrounding `on_ready` body first and place this where `_STATE` is loaded and `channel` is the officer channel; adapt the variable names to what is actually there rather than assuming.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Enable the members intent and drop stranded special requests

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Raffle configuration and gating

**Files:**
- Modify: `items_bot.py` (append commands near `setqueuechannel_cmd`)
- Test: `tests/test_items_bot.py` (append)

**Interfaces:**
- Consumes: `items_state.State.raffle_role_ids`, `.raffle_channel_id` from Task 4; `items_bot.save_state`, `ok_embed`, `error_embed` (existing).
- Produces:
  - `items_bot.has_raffle_role(author, role_ids) -> bool`
  - `items_bot.raffle_access(ctx) -> str | None` — `None` when permitted, otherwise a refusal message; returns the sentinel `items_bot.IGNORE` when the command should be silently dropped.
  - `items_bot.IGNORE: str`
  - `items_bot.setraffleroles_cmd`, `items_bot.setrafflechannel_cmd`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_bot.py`:

```python
class FakeRole:
    def __init__(self, role_id):
        self.id = role_id
        self.mention = f"@role-{role_id}"


class FakeMember:
    def __init__(self, user_id=1, roles=(), display_name="BK | Jjew", administrator=False):
        self.id = user_id
        self.display_name = display_name
        self.roles = list(roles)
        self.guild_permissions = type(
            "Perms", (), {"administrator": administrator}
        )()


def _raffle_ctx(channel_id=42, roles=(10,), administrator=False):
    channel = FakeChannel(channel_id)
    ctx = FakeCtx(channel)
    ctx.author = FakeMember(roles=[FakeRole(r) for r in roles], administrator=administrator)
    return ctx, channel


def test_holding_any_configured_role_is_enough():
    author = FakeMember(roles=[FakeRole(99), FakeRole(11)])

    assert items_bot.has_raffle_role(author, [10, 11])


def test_holding_no_configured_role_is_refused():
    author = FakeMember(roles=[FakeRole(99)])

    assert not items_bot.has_raffle_role(author, [10, 11])


def test_setraffleroles_stores_every_role_once():
    items_bot._STATE.officer_channel_id = 1
    channel = FakeChannel(1)
    ctx = FakeCtx(channel)
    roles = (FakeRole(10), FakeRole(11), FakeRole(10))

    asyncio.run(items_bot.setraffleroles_cmd.callback(ctx, *roles))

    assert items_bot._STATE.raffle_role_ids == [10, 11]
    assert ctx.sent[-1]["embed"].title == "✅ Raffle roles set"


def test_setraffleroles_without_a_role_shows_usage():
    items_bot._STATE.officer_channel_id = 1
    ctx = FakeCtx(FakeChannel(1))

    asyncio.run(items_bot.setraffleroles_cmd.callback(ctx))

    assert items_bot._STATE.raffle_role_ids == []
    assert "!setraffleroles" in ctx.sent[-1]["embed"].description


def test_setrafflechannel_requires_an_officer_channel_first():
    ctx = FakeCtx(FakeChannel(42))

    asyncio.run(items_bot.setrafflechannel_cmd.callback(ctx))

    assert items_bot._STATE.raffle_channel_id is None
    assert "!setofficerchannel" in ctx.sent[-1]["embed"].description


def test_setrafflechannel_records_the_channel(monkeypatch):
    state_channel = FakeChannel(1)
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda channel_id: state_channel)
    items_bot._STATE.officer_channel_id = 1
    ctx = FakeCtx(FakeChannel(42))

    asyncio.run(items_bot.setrafflechannel_cmd.callback(ctx))

    assert items_bot._STATE.raffle_channel_id == 42
    assert ctx.sent[-1]["embed"].title == "✅ Raffle channel set"


def test_an_unconfigured_raffle_channel_hints_only_to_admins():
    admin_ctx, _ = _raffle_ctx(administrator=True)
    member_ctx, _ = _raffle_ctx(administrator=False)

    assert "!setrafflechannel" in items_bot.raffle_access(admin_ctx)
    assert items_bot.raffle_access(member_ctx) is items_bot.IGNORE


def test_the_wrong_channel_is_silently_ignored():
    items_bot._STATE.raffle_channel_id = 42
    items_bot._STATE.raffle_role_ids = [10]
    ctx, _ = _raffle_ctx(channel_id=999)

    assert items_bot.raffle_access(ctx) is items_bot.IGNORE


def test_no_configured_roles_is_a_refusal_not_an_open_door():
    items_bot._STATE.raffle_channel_id = 42
    ctx, _ = _raffle_ctx()

    assert "!setraffleroles" in items_bot.raffle_access(ctx)


def test_a_member_without_a_raffle_role_is_refused():
    items_bot._STATE.raffle_channel_id = 42
    items_bot._STATE.raffle_role_ids = [10]
    ctx, _ = _raffle_ctx(roles=(99,))

    assert "role" in items_bot.raffle_access(ctx).casefold()


def test_a_role_holder_in_the_raffle_channel_is_permitted():
    items_bot._STATE.raffle_channel_id = 42
    items_bot._STATE.raffle_role_ids = [10]
    ctx, _ = _raffle_ctx()

    assert items_bot.raffle_access(ctx) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -k "raffle_role or setraffle or raffle_access or raffle_channel or open_door or silently_ignored" -v`
Expected: FAIL — `AttributeError: module 'items_bot' has no attribute 'has_raffle_role'`

- [ ] **Step 3: Write the implementation**

Add to `items_bot.py`, after `setqueuechannel_cmd`:

```python
# raffle_access returns this instead of a message when the command should
# produce no reply at all. A distinct object rather than None, because
# None already means "permitted".
IGNORE = "\x00ignore"


def has_raffle_role(author, role_ids: list[int]) -> bool:
    """True if the author holds ANY configured raffle role."""
    wanted = set(role_ids or ())
    if not wanted:
        return False
    return any(role.id in wanted for role in getattr(author, "roles", []))


def raffle_access(ctx) -> str | None:
    """None if this raffle command may run, else a refusal or IGNORE.

    Wrong-channel is silent: a raffle command typed elsewhere is far more
    likely to be a typo than an attack, and a reply would only advertise
    that the channel exists. The unconfigured case is the exception --
    silence there is a dead end, so the one person who can fix it is told
    and nobody else is.
    """
    if _STATE.raffle_channel_id is None:
        permissions = getattr(ctx.author, "guild_permissions", None)
        if getattr(permissions, "administrator", False):
            return (
                "No raffle channel is set. Run `!setrafflechannel` in the "
                "channel where special log polls should be posted."
            )
        return IGNORE

    if ctx.channel.id != _STATE.raffle_channel_id:
        return IGNORE

    if not _STATE.raffle_role_ids:
        return (
            "No raffle role is set. An admin must run "
            "`!setraffleroles @role` before the raffle commands work."
        )
    if not has_raffle_role(ctx.author, _STATE.raffle_role_ids):
        return "You need a raffle role to run this command."
    return None


async def _refuse_raffle(ctx, verdict: str) -> bool:
    """Send the refusal if there is one. True when the caller must stop."""
    if verdict is None:
        return False
    if verdict is not IGNORE:
        await ctx.send(embed=error_embed("Not allowed", verdict))
    return True


@bot.command(name="setraffleroles")
@commands.has_permissions(administrator=True)
async def setraffleroles_cmd(ctx, *roles: discord.Role):
    """Choose which roles may run the raffle commands."""
    if not roles:
        await ctx.send(
            embed=error_embed(
                "Which role?",
                "Usage: `!setraffleroles @role [@role ...]`\n"
                "Every role you list replaces the current set.",
            )
        )
        return

    # Deduplicated by id, order preserved, so mentioning a role twice
    # does not store it twice. Keyed on the id rather than the object so
    # this never depends on Role being hashable.
    unique: dict[int, discord.Role] = {}
    for role in roles:
        unique.setdefault(role.id, role)
    _STATE.raffle_role_ids = list(unique)

    channel = (
        bot.get_channel(_STATE.officer_channel_id)
        if _STATE.officer_channel_id is not None
        else None
    )
    if channel is not None:
        await save_state(channel)
    mentions = ", ".join(role.mention for role in unique.values())
    await ctx.send(
        embed=ok_embed(
            "Raffle roles set",
            f"{mentions} can now run `!poll`, `!list` and `!winner`.",
        )
    )


@bot.command(name="setrafflechannel")
@commands.has_permissions(administrator=True)
async def setrafflechannel_cmd(ctx):
    """Record this channel as the special log raffle channel."""
    if _STATE.officer_channel_id is None:
        await ctx.send(
            embed=error_embed(
                "Not set up yet",
                "An admin must run `!setofficerchannel` in the officers' "
                "channel before a raffle channel can be set.",
            )
        )
        return

    _STATE.raffle_channel_id = ctx.channel.id
    channel = bot.get_channel(_STATE.officer_channel_id)
    if channel is not None:
        await save_state(channel)
    await ctx.send(
        embed=ok_embed(
            "Raffle channel set",
            f"`!poll`, `!list` and `!winner` now work in {ctx.channel.mention}.",
        )
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Gate the raffle commands on a channel and configured roles

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: `!poll`

**Files:**
- Modify: `items_bot.py` (append)
- Test: `tests/test_items_bot.py` (append)

**Interfaces:**
- Consumes: `items_raffle.parse_poll_argument`, `items_rules.resolve_special`, `items_state.evict_for_new_raffle`, `items_state.find_raffle`, `items_state.Raffle`, `items_sheet.read_snapshot`.
- Produces: `items_bot.poll_cmd`, `items_bot.build_poll(item, hours) -> discord.Poll`

- [ ] **Step 1: Write the failing tests**

First extend the fakes. Add to `tests/test_items_bot.py`:

```python
class FakePollAnswer:
    def __init__(self, text, voters=()):
        self.text = text
        self._voters = list(voters)

    def voters(self, **kwargs):
        async def _iterator():
            for voter in self._voters:
                yield voter

        return _iterator()


class FakePoll:
    def __init__(self, question="Asta's Heart", answers=None, finalised=True):
        self.question = question
        self.answers = answers if answers is not None else [FakePollAnswer("Yes")]
        self._finalised = finalised

    def is_finalised(self):
        return self._finalised
```

Then add `self.poll = None` to `FakeMessage.__init__` and `message.poll = kwargs.get("poll")` inside `FakeChannel.send` (beside the existing `embed` and `view` lines).

Add the tests:

```python
def _sheet(monkeypatch, special=("Player Name", "Asta's Heart"), gear=("Player Name", "Sacred Ring"), roster=("Jjew", "Kobe"), holds=()):
    snapshot = items_sheet.Snapshot(
        roster=list(roster),
        special_headers=list(special),
        gear_headers=list(gear),
        ledger_rows=[],
        special_grid=[],
    )
    monkeypatch.setattr(items_sheet, "read_snapshot", lambda spreadsheet: snapshot)
    monkeypatch.setattr(
        items_sheet, "holds_special",
        lambda snap, ign, item: ign in holds,
    )
    return snapshot


def _configured_raffle(monkeypatch, channel_id=42):
    state_channel = FakeChannel(1)
    items_bot._STATE.officer_channel_id = 1
    items_bot._STATE.raffle_channel_id = channel_id
    items_bot._STATE.raffle_role_ids = [10]
    monkeypatch.setattr(items_bot.bot, "get_channel", lambda cid: state_channel)
    return state_channel


def _posted_poll(channel):
    """The poll message, not the confirmation embed sent after it.

    poll_cmd sends two messages: the poll itself, then an ok_embed
    telling the officer when it closes. channel.sent[-1] is the latter.
    """
    polls = [message for message in channel.sent if message.poll is not None]
    assert len(polls) == 1, f"expected one poll message, got {len(polls)}"
    return polls[0]


def test_poll_posts_a_poll_and_records_the_raffle(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    posted = _posted_poll(channel)
    assert posted.poll.question == "Asta's Heart"
    assert [a.text for a in posted.poll.answers] == ["Yes"]
    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.message_id == posted.id
    assert raffle.channel_id == channel.id
    assert raffle.listed is False


def test_poll_defaults_to_twenty_four_hours(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert _posted_poll(channel).poll.duration == datetime.timedelta(hours=24)


def test_poll_honours_the_hours_flag(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart --hours 48"))

    assert _posted_poll(channel).poll.duration == datetime.timedelta(hours=48)


def test_poll_refuses_a_gear_log(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Sacred Ring"))

    assert "!request" in ctx.sent[-1]["embed"].description
    assert items_bot._STATE.raffles == []


def test_poll_refuses_a_second_open_raffle_for_the_same_log(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))
    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert "already open" in ctx.sent[-1]["embed"].description
    assert len(items_bot._STATE.raffles) == 1


def test_poll_refuses_when_every_slot_holds_a_live_raffle(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, special=("Player Name", "Asta's Heart", *[f"Log {n}" for n in range(5)]))
    items_bot._STATE.raffles = [
        items_state.Raffle(
            item=f"Log {n}", channel_id=42, message_id=n,
            created_at="2026-08-09 10:00:00", ends_at="2099-01-01 00:00:00",
        )
        for n in range(items_state.MAX_RAFFLES)
    ]
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert "still open" in ctx.sent[-1]["embed"].description
    assert len(items_bot._STATE.raffles) == items_state.MAX_RAFFLES


def test_poll_outside_the_raffle_channel_says_nothing(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx(channel_id=999)

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart"))

    assert ctx.sent == []
```

Add `import datetime` and `import items_sheet` to the test module's imports if not already present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -k poll -v`
Expected: FAIL — `AttributeError: module 'items_bot' has no attribute 'poll_cmd'`

- [ ] **Step 3: Write the implementation**

Add `import datetime` and `import items_raffle` to `items_bot.py`'s imports, then append:

```python
POLL_ANSWER = "Yes"


def build_poll(item: str, hours: int) -> discord.Poll:
    """A single-answer poll: voting is entering, so there is nothing to read."""
    poll = discord.Poll(
        question=item, duration=datetime.timedelta(hours=hours)
    )
    poll.add_answer(text=POLL_ANSWER)
    return poll


@bot.command(name="poll")
async def poll_cmd(ctx, *, argument: str = ""):
    """Open a raffle for one special log."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    try:
        parsed = items_raffle.parse_poll_argument(argument)
    except items_raffle.RaffleArgumentError as exc:
        await ctx.send(embed=error_embed("Poll refused", str(exc)))
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        try:
            item = items_rules.resolve_special(
                parsed.item_query, snapshot.special_headers, snapshot.gear_headers
            )
        except items_rules.ItemLookupError as exc:
            await ctx.send(embed=error_embed("Poll refused", str(exc)))
            return

        now = items_rules.now_pht()
        now_text = items_rules.format_timestamp(now)
        existing = items_state.find_raffle(_STATE, item)
        if existing is not None and existing.ends_at > now_text and not existing.winner:
            await ctx.send(
                embed=error_embed(
                    "Poll refused",
                    f"A raffle for **{item}** is already open. It closes at "
                    f"{existing.ends_at} PHT.",
                )
            )
            return

        if not items_state.evict_for_new_raffle(_STATE, now_text):
            await ctx.send(
                embed=error_embed(
                    "Poll refused",
                    f"All {items_state.MAX_RAFFLES} tracked raffles are still "
                    "open. Draw a winner for one of them first.",
                )
            )
            return

        try:
            message = await ctx.channel.send(poll=build_poll(item, parsed.hours))
        except Exception as exc:
            await ctx.send(embed=error_embed("Could not post the poll", str(exc)))
            return

        # Recorded only once Discord has confirmed the message, so a
        # failed post can never leave a raffle pointing at nothing.
        raffle = items_state.Raffle(
            item=item,
            channel_id=ctx.channel.id,
            message_id=message.id,
            created_at=now_text,
            ends_at=items_rules.format_timestamp(
                now + datetime.timedelta(hours=parsed.hours)
            ),
        )
        _STATE.raffles.append(raffle)

        if not items_state.fits(_STATE):
            _STATE.raffles.remove(raffle)
            await ctx.send(
                embed=error_embed(
                    "Poll not recorded",
                    "The bot's storage is full, so this raffle could not be "
                    "saved. The poll above will not be tracked -- delete it, "
                    "clear the request queue, and try again.",
                )
            )
            return

        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

    await ctx.send(
        embed=ok_embed(
            "Raffle open",
            f"**{item}** — answer **{POLL_ANSWER}** above to enter. Closes at "
            f"{raffle.ends_at} PHT. Run `!list {item}` after that.",
        )
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Open a special log raffle with !poll

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: `!list`

**Files:**
- Modify: `items_bot.py` (append)
- Test: `tests/test_items_bot.py` (append)

**Interfaces:**
- Consumes: `items_raffle.classify_voters`, `items_raffle.Voter`, `items_state.find_raffle`, `items_state.replace_raffle`, `items_sheet.holds_special`.
- Produces:
  - `items_bot.poll_voters(message) -> list[items_raffle.Voter]`
  - `items_bot.render_pool(item, split, winner) -> str`
  - `items_bot.list_cmd`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_bot.py`:

```python
class FakeVoter:
    def __init__(self, user_id, display_name):
        self.id = user_id
        self.display_name = display_name


def _open_raffle(channel, item="Asta's Heart", ends="2099-01-01 00:00:00", **kwargs):
    poll = kwargs.pop("poll", FakePoll(question=item))
    message = FakeMessage(message_id=555)
    message.poll = poll
    channel._pins.append(message)
    raffle = items_state.Raffle(
        item=item, channel_id=channel.id, message_id=message.id,
        created_at="2026-08-09 10:00:00", ends_at=ends, **kwargs,
    )
    items_bot._STATE.raffles.append(raffle)
    return raffle, message


def test_list_refuses_while_the_poll_is_still_open(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel)  # ends_at defaults to 2099, so the poll is live

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    assert ctx.sent[-1]["embed"].title == "❌ Poll still open"
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is False


def test_list_splits_the_voters_and_freezes_the_pool(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"), holds=("Kobe",))
    ctx, channel = _raffle_ctx()
    answer = FakePollAnswer(
        "Yes",
        [FakeVoter(1, "BK | Jjew"), FakeVoter(2, "M2 | Kobe"), FakeVoter(3, "Stranger")],
    )
    _open_raffle(channel, ends="2026-08-09 10:00:00", poll=FakePoll(answers=[answer]))

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is True
    assert raffle.eligible == ("Jjew",)
    description = ctx.sent[-1]["embed"].description
    assert "Jjew" in description
    assert "Kobe" in description
    assert "<@3>" in description


def test_listing_twice_replays_the_frozen_pool(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True
    )
    channel._pins[-1].poll = FakePoll(answers=[FakePollAnswer("Yes", [FakeVoter(9, "Kobe")])])

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    assert "Jjew" in ctx.sent[-1]["embed"].description
    assert "Kobe" not in ctx.sent[-1]["embed"].description


def test_list_refuses_an_unknown_raffle(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Benji's Heart"))

    assert "No raffle" in ctx.sent[-1]["embed"].description


def test_list_refuses_when_the_poll_message_is_gone(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(channel, ends="2026-08-09 10:00:00")
    message.deleted = True

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    assert "poll message" in ctx.sent[-1]["embed"].description


def test_a_deleted_poll_message_does_not_matter_once_listed(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _, message = _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True
    )
    message.deleted = True

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    assert "Jjew" in ctx.sent[-1]["embed"].description


def test_list_shows_the_winner_once_drawn(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True, winner="Jjew"
    )

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    assert "Winner" in ctx.sent[-1]["embed"].description
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -k list_ -v`
Expected: FAIL — `AttributeError: module 'items_bot' has no attribute 'list_cmd'`

- [ ] **Step 3: Write the implementation**

Append to `items_bot.py`:

```python
async def poll_voters(message) -> list[items_raffle.Voter]:
    """Everyone who answered Yes, as (id, nickname) pairs.

    The answer is found by its text rather than by index: an id is only
    stable for a poll this bot created, and a raffle recorded before a
    restart must still be readable.

    A voter arrives as a Member when the members intent is on and the
    guild is cached, and as a User otherwise. Only the Member carries the
    server nickname, so a User is looked up once over HTTP before giving
    up on the global name.
    """
    poll = getattr(message, "poll", None)
    if poll is None or not poll.answers:
        raise LookupError("that message no longer carries a poll")

    answer = next(
        (a for a in poll.answers if a.text.strip().casefold() == POLL_ANSWER.casefold()),
        poll.answers[0],
    )

    guild = getattr(message, "guild", None)
    voters: list[items_raffle.Voter] = []
    async for voter in answer.voters():
        display_name = getattr(voter, "display_name", "")
        if guild is not None and not isinstance(voter, discord.Member):
            try:
                member = await guild.fetch_member(voter.id)
            except Exception:
                member = None
            if member is not None:
                display_name = member.display_name
        voters.append(
            items_raffle.Voter(user_id=voter.id, display_name=display_name)
        )
    return voters


def render_pool(item: str, split: items_raffle.VoterSplit, winner: str = "") -> str:
    """The three groups an officer needs, in one description."""
    lines = [f"**Eligible for {item}** ({len(split.eligible)})"]
    lines.append(
        "\n".join(f"{n}. {ign}" for n, ign in enumerate(split.eligible, start=1))
        or "_nobody_"
    )
    if split.already_have:
        lines.append("")
        lines.append("**Already has it** (excluded)")
        lines.append(", ".join(split.already_have))
    if split.unidentified:
        lines.append("")
        lines.append("**Couldn't identify** — sort these out by hand")
        lines.append(" ".join(f"<@{voter.user_id}>" for voter in split.unidentified))
    if winner:
        lines.append("")
        lines.append(f"🏆 **Winner: {winner}**")
    return "\n".join(lines)


@bot.command(name="list")
async def list_cmd(ctx, *, argument: str = ""):
    """Show who is eligible for a closed raffle."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    item_query = argument.strip()
    raffle = items_state.find_raffle(_STATE, item_query) if item_query else None
    if raffle is None:
        await ctx.send(
            embed=error_embed(
                "Nothing to list",
                f"No raffle for {item_query!r}. Usage: `!list <special log name>`",
            )
        )
        return

    if raffle.listed:
        # Replayed verbatim, never recomputed: the pool a winner is drawn
        # from must not be able to change between looking and drawing.
        split = items_raffle.VoterSplit(eligible=list(raffle.eligible))
        await ctx.send(
            embed=ok_embed(
                f"Raffle: {raffle.item}", render_pool(raffle.item, split, raffle.winner)
            )
        )
        return

    now = items_rules.format_timestamp(items_rules.now_pht())
    if raffle.ends_at > now:
        await ctx.send(
            embed=error_embed(
                "Poll still open",
                f"**{raffle.item}** closes at {raffle.ends_at} PHT. "
                "Drawing before then would leave out anyone who has not voted.",
            )
        )
        return

    try:
        message = await ctx.channel.fetch_message(raffle.message_id)
        voters = await poll_voters(message)
    except Exception as exc:
        await ctx.send(
            embed=error_embed(
                "Cannot read the poll",
                f"The poll message for **{raffle.item}** could not be read "
                f"({exc}). Run `!poll {raffle.item}` again to hold a new one.",
            )
        )
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        split = items_raffle.classify_voters(
            voters,
            snapshot.roster,
            holds=lambda ign: items_sheet.holds_special(snapshot, ign, raffle.item),
        )
        updated = items_state.replace_raffle(
            _STATE, raffle, eligible=tuple(split.eligible), listed=True
        )
        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

    await ctx.send(
        embed=ok_embed(
            f"Raffle: {updated.item}", render_pool(updated.item, split, updated.winner)
        )
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Show the eligible pool with !list and freeze it

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: `!winner`

**Files:**
- Modify: `items_bot.py` (append)
- Test: `tests/test_items_bot.py` (append)

**Interfaces:**
- Consumes: `items_raffle.split_item_and_ign`, `items_state.find_raffle`, `items_state.replace_raffle`, `items_state.raffle_item_names`, `items_state.new_request_id`, `items_sheet.commit_approval`, `items_sheet.LedgerWriteError`.
- Produces: `items_bot.winner_cmd`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_bot.py`:

```python
def test_winner_ticks_the_checkbox_and_closes_the_raffle(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe"), listed=True)
    calls = {}

    def _commit(spreadsheet, **kwargs):
        calls.update(kwargs)
        return "C4"

    monkeypatch.setattr(items_sheet, "commit_approval", _commit)

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew"))

    assert calls["ign"] == "Jjew"
    assert calls["item"] == "Asta's Heart"
    assert calls["item_type"] == items_rules.SPECIAL
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winner == "Jjew"
    assert ctx.sent[-1]["embed"].title == "✅ Winner recorded"


def test_winner_refuses_a_player_not_on_the_frozen_list(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Kobe"))

    assert "not on the eligible list" in ctx.sent[-1]["embed"].description
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winner == ""


def test_winner_refuses_before_list_has_been_run(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew"))

    assert "!list" in ctx.sent[-1]["embed"].description


def test_winner_refuses_while_the_poll_is_open(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, eligible=("Jjew",), listed=True)
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew"))

    assert ctx.sent[-1]["embed"].title == "❌ Poll still open"


def test_winner_refuses_a_second_draw(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe"),
        listed=True, winner="Kobe",
    )
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew"))

    assert "already been drawn" in ctx.sent[-1]["embed"].description


def test_winner_refuses_an_unknown_raffle(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Benji's Heart Jjew"))

    assert "No open raffle" in ctx.sent[-1]["embed"].description


def test_a_failed_sheet_write_leaves_the_raffle_open(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)

    def _boom(spreadsheet, **kwargs):
        raise RuntimeError("Sheets is down")

    monkeypatch.setattr(items_sheet, "commit_approval", _boom)

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew"))

    assert "Sheets is down" in ctx.sent[-1]["embed"].description
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winner == ""


def test_a_ledger_failure_closes_the_raffle_and_hands_over_the_row(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)
    row = ["2026-08-09 12:00:00", "Jjew", "Asta's Heart", "Special", "Keith", "1", "abc"]

    def _ledger_failure(spreadsheet, **kwargs):
        raise items_sheet.LedgerWriteError("C4", row, RuntimeError("append failed"))

    monkeypatch.setattr(items_sheet, "commit_approval", _ledger_failure)

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew"))

    description = ctx.sent[-1]["embed"].description
    assert "C4" in description
    assert "Jjew" in description
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winner == "Jjew"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -k winner -v`
Expected: FAIL — `AttributeError: module 'items_bot' has no attribute 'winner_cmd'`

- [ ] **Step 3: Write the implementation**

Append to `items_bot.py`:

```python
@bot.command(name="winner")
async def winner_cmd(ctx, *, argument: str = ""):
    """Record the winner of a closed raffle."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        try:
            item, ign = items_raffle.split_item_and_ign(
                argument, items_state.raffle_item_names(_STATE), snapshot.roster
            )
        except items_raffle.RaffleArgumentError as exc:
            await ctx.send(embed=error_embed("Winner refused", str(exc)))
            return

        raffle = items_state.find_raffle(_STATE, item)
        now = items_rules.format_timestamp(items_rules.now_pht())

        if raffle.winner:
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"**{raffle.item}** has already been drawn: "
                    f"**{raffle.winner}** won it.",
                )
            )
            return
        if raffle.ends_at > now:
            await ctx.send(
                embed=error_embed(
                    "Poll still open",
                    f"**{raffle.item}** closes at {raffle.ends_at} PHT.",
                )
            )
            return
        if not raffle.listed:
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"Run `!list {raffle.item}` first. The winner is checked "
                    "against the eligible list that command freezes.",
                )
            )
            return

        wanted = items_rules.normalize(ign)
        on_list = next(
            (name for name in raffle.eligible if items_rules.normalize(name) == wanted),
            None,
        )
        if on_list is None:
            suggestions = get_close_matches(ign, list(raffle.eligible), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"**{ign}** is not on the eligible list for "
                    f"**{raffle.item}**.{hint}",
                )
            )
            return

        try:
            await asyncio.to_thread(
                lambda: items_sheet.commit_approval(
                    _SPREADSHEET,
                    ign=on_list,
                    item=raffle.item,
                    item_type=items_rules.SPECIAL,
                    timestamp=now,
                    officer=getattr(ctx.author, "display_name", str(ctx.author)),
                    user_id=ctx.author.id,
                    request_id=items_state.new_request_id(),
                )
            )
        except items_sheet.LedgerWriteError as exc:
            # The checkbox IS ticked. Re-running could only fail against
            # a ticked box, so the raffle closes and the officer is given
            # the exact ledger row instead.
            items_state.replace_raffle(_STATE, raffle, winner=on_list)
            channel = (
                bot.get_channel(_STATE.officer_channel_id)
                if _STATE.officer_channel_id is not None
                else None
            )
            if channel is not None:
                await save_state(channel)
            pasteable = " | ".join(exc.row)
            await ctx.send(
                embed=error_embed(
                    "Winner recorded, ledger not",
                    f"**{on_list}** won **{raffle.item}** and cell "
                    f"{exc.address} is ticked, but the Distribution Log row "
                    f"could not be written: {exc}\nDo NOT run this again — "
                    f"add this row to `{items_sheet.LEDGER_TAB}` by hand:\n"
                    f"```\n{pasteable}\n```",
                )
            )
            return
        except Exception as exc:
            await ctx.send(
                embed=error_embed(
                    "Sheet write failed",
                    f"Nothing was recorded, the raffle is still open: {exc}",
                )
            )
            return

        items_state.replace_raffle(_STATE, raffle, winner=on_list)
        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

    await ctx.send(
        embed=ok_embed(
            "Winner recorded",
            f"🏆 **{on_list}** wins **{raffle.item}**. Their checkbox in "
            f"`{items_sheet.SPECIAL_TAB}` is ticked, so they will not be "
            "eligible for this log again.",
        )
    )
```

Add `from difflib import get_close_matches` to `items_bot.py`'s imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS — the whole suite.

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Record a raffle winner and tick their special log

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Help text and documentation

**Files:**
- Modify: `items_bot.py:1002-1021` (`itemhelp_cmd`)
- Modify: `README.md`
- Modify: `docs/item-bot-setup.md`
- Test: `tests/test_items_bot.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 5–10.
- Produces: no new callables.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_items_bot.py`:

```python
def test_help_says_request_is_gear_only_and_lists_the_raffle_commands():
    ctx = FakeCtx(FakeChannel(1))

    asyncio.run(items_bot.itemhelp_cmd.callback(ctx))

    text = str(ctx.sent[-1]["embed"].to_dict())
    assert "gear" in text.casefold()
    for command in ("!poll", "!list", "!winner", "!setraffleroles", "!setrafflechannel"):
        assert command in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_items_bot.py -k help_says -v`
Expected: FAIL — `assert '!poll' in ...`

- [ ] **Step 3: Write the implementation**

Read `itemhelp_cmd` as it stands, then rewrite its fields so that:

- `!request <item name> <IGN>` is described as **gear logs only**, with a line saying special logs are raffled in the raffle channel.
- A "Special log raffle" field lists `!poll <special log> [--hours N]`, `!list <special log>`, `!winner <special log> <IGN>`, marked as raffle-role only.
- The admin field gains `!setraffleroles @role [@role ...]` and `!setrafflechannel`.

Then update `README.md` and `docs/item-bot-setup.md`:

- Describe the raffle flow: `!poll` → members answer Yes → poll closes → `!list` → `!winner`.
- State that `!request` handles gear logs only.
- Document the two new admin commands and the order they must be run in (`!setofficerchannel` first).
- Add the **Server Members Intent** to the setup checklist, spelled out as a toggle in the Discord Developer Portal, with the consequence of leaving it off: every voter is reported as unidentified.
- Note that nicknames must contain the IGN (`BK | Jjew`, `M2 - Jjew`, or a bare `Jjew`) or the member cannot be resolved.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py README.md docs/item-bot-setup.md tests/test_items_bot.py
git commit -m "Document the special log raffle

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification before merge

- [ ] `.venv/bin/python -m pytest` — whole suite green, with the count of tests reported.
- [ ] `.venv/bin/python -c "import items_bot, items_raffle, items_state, items_rules, items_sheet"` — every module imports.
- [ ] `grep -rn "resolve_item" --include=*.py .` — no caller still expects a `SPECIAL` result.
- [ ] Confirm the Server Members Intent is enabled in the Discord Developer Portal before deploying; without it the raffle identifies nobody.
