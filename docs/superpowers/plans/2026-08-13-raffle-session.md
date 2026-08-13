# Raffle Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `!list` and `!winner` with a guided raffle session (`!startraffle` → `!won` / `!skipraffle`) that walks every closed poll in turn and stops any player from winning twice in one sitting.

**Architecture:** The session is a frozen dataclass persisted in the bot's pinned Discord state, holding the ordered log names, the current position, and the per-log results. Pure decisions (name parsing, pool filtering, candidate selection) live in `items_raffle.py` and `items_state.py` and are unit tested without Discord or Google Sheets. `items_bot.py` keeps only the I/O and the orchestration, built from two helpers extracted from the commands being deleted, so the pool-freezing and sheet-writing logic exists exactly once.

**Tech Stack:** Python 3.11+, discord.py 2.7, gspread, pytest. No new dependencies.

## Global Constraints

- **Read the spec first:** `docs/superpowers/specs/2026-08-13-raffle-session-design.md`. It is the authority on behaviour; this plan is the authority on code.
- **The bot never draws.** No `random`, no `secrets.choice`, nothing that picks a name. The officer draws by hand. The bot decides who *may* be picked and records who *was*.
- **Every task leaves the full test suite green.** Run `python -m pytest tests/ -q` before every commit. A task that breaks an existing test either fixes that test as part of the task or is in the wrong order.
- **Name comparison is always `items_rules.normalize(...)`**, never a raw string compare. It is `attendance_roster.normalize` re-exported: NFKC + casefold + whitespace collapse. Raw compares let an alias slip a session winner back into a later pool.
- **Nothing is written to the sheet until every name in the command has been validated.** A typo in the third name must not leave the first two ticked.
- **State that must survive a restart goes in the pin.** The bot runs on Render's free tier and restarts. In-memory session state would take the "already won" list with it.
- **Sheet writes and state mutation happen under `items_bot._SHEET_LOCK`.** `asyncio.Lock` is **not** reentrant — a helper that takes the lock must never be called by a function already holding it. Where this plan says "outside the lock", that is why.
- **Timestamps** are `items_rules.format_timestamp(items_rules.now_pht())`, compared as strings (the format sorts lexicographically).
- Follow the surrounding comment style: comments explain *why* a decision was made, not what the line does.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `items_raffle.py` | Pure raffle logic: identity resolution, argument parsing, pool arithmetic | Add `split_igns`, `remaining_pool`; later delete `split_item_and_ign`, `split_item_and_igns`, `WINNER_USAGE` |
| `items_state.py` | Persisted state and queries over it | Add `RaffleSession`, `State.raffle_session`, encode/decode, `session_candidates` |
| `items_bot.py` | Discord commands and I/O orchestration | Extract `_freeze_raffle` and `_record_winners`; add `!startraffle`, `!won`, `!skipraffle`, `_post_current_poll`; delete `list_cmd`, `winner_cmd` |
| `tests/test_items_raffle.py` | Pure logic tests | Add `split_igns` / `remaining_pool`; later delete the `split_item_and_*` tests |
| `tests/test_items_state.py` | Persistence tests | Session round-trip, legacy pin, `fits` |
| `tests/test_items_bot.py` | Command wiring tests | Session walkthrough and failure paths; later delete `list`/`winner` tests |
| `README.md`, `docs/item-bot-setup.md` | Operator docs | Rewrite the raffle sections |

---

### Task 1: `split_igns` — parse a bare list of winner names

`!won` gets only names, never a log name, so the ~120 lines that exist to answer "where does the log name end and the IGN begin?" are not needed. This is the replacement. The old functions stay in place for now so `winner_cmd` keeps working; Task 11 deletes them.

**Files:**
- Modify: `items_raffle.py` (add after `split_item_and_igns`, near the existing `WINNER_SPLIT` regex)
- Test: `tests/test_items_raffle.py`

**Interfaces:**
- Consumes: `items_rules.resolve_ign`, `items_rules.normalize`, `items_raffle.WINNER_SPLIT`, `items_raffle.DANGLING_SEPARATOR`, `items_raffle.RaffleArgumentError`
- Produces: `items_raffle.WON_USAGE: str`, `items_raffle.split_igns(argument: str, roster: list[str]) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_items_raffle.py` (the module already defines `ROSTER`; check its value at the top of the file and add any names these tests need to that constant rather than redefining it):

```python
def test_split_igns_reads_one_name():
    assert items_raffle.split_igns("Jjew", ROSTER) == ["Jjew"]


def test_split_igns_reads_several_names():
    assert items_raffle.split_igns("Jjew - Kobe", ROSTER) == ["Jjew", "Kobe"]


def test_split_igns_keeps_a_hyphenated_name_intact():
    """A separator needs whitespace on BOTH sides; 'wile-KAMOTE' is a row."""
    assert items_raffle.split_igns("wile-KAMOTE", ROSTER) == ["wile-KAMOTE"]
    assert items_raffle.split_igns("wile-KAMOTE - Jjew", ROSTER) == [
        "wile-KAMOTE",
        "Jjew",
    ]


def test_split_igns_keeps_a_multi_word_name_intact():
    assert items_raffle.split_igns("chinchong ni Mumu", ROSTER) == [
        "chinchong ni Mumu"
    ]


def test_split_igns_returns_the_roster_spelling():
    assert items_raffle.split_igns("jjew", ROSTER) == ["Jjew"]


def test_split_igns_refuses_an_empty_argument():
    with pytest.raises(items_raffle.RaffleArgumentError):
        items_raffle.split_igns("   ", ROSTER)


def test_split_igns_refuses_a_dangling_separator():
    with pytest.raises(items_raffle.RaffleArgumentError, match="empty name"):
        items_raffle.split_igns("Jjew - ", ROSTER)


def test_split_igns_refuses_an_unknown_name_with_a_suggestion():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Did you mean"):
        items_raffle.split_igns("Jjeww", ROSTER)


def test_split_igns_refuses_the_same_player_twice():
    with pytest.raises(items_raffle.RaffleArgumentError, match="more than once"):
        items_raffle.split_igns("Jjew - jjew", ROSTER)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_raffle.py -k split_igns -q`
Expected: FAIL, `AttributeError: module 'items_raffle' has no attribute 'split_igns'`

- [ ] **Step 3: Implement `split_igns`**

Add to `items_raffle.py`, after `split_item_and_igns`:

```python
WON_USAGE = (
    "Usage: `!won <IGN>`, or `!won <IGN> - <IGN> - <IGN>` for several "
    "winners of the same log."
)


def split_igns(argument: str, roster: list[str]) -> list[str]:
    """The winners named in a `!won` argument, in roster spelling.

    The session supplies the log, so this parses names only -- none of
    the item/IGN boundary guessing `!winner` needed.

    Every name is resolved before this returns, so a typo in the third
    name is refused before any checkbox is ticked rather than half way
    through.
    """
    text = argument.strip()
    if not text:
        raise RaffleArgumentError(f"Which player won? {WON_USAGE}")

    chunks = WINNER_SPLIT.split(text)
    # A separator needs whitespace on both sides, so only a hyphen with
    # whitespace BEFORE it is a dangling one. Testing the last character
    # alone would reject a roster name that simply ends in a hyphen.
    if DANGLING_SEPARATOR.search(argument) or any(
        not chunk.strip() for chunk in chunks
    ):
        raise RaffleArgumentError(
            f"There is an empty name between two dashes. {WON_USAGE}"
        )

    igns: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        try:
            player = items_rules.resolve_ign(chunk, roster)
        except items_rules.RequestParseError as exc:
            raise RaffleArgumentError(str(exc)) from None
        if player is None:
            suggestions = get_close_matches(chunk, roster, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise RaffleArgumentError(
                f"No player named {chunk!r} in the sheet.{hint} {WON_USAGE}"
            )
        igns.append(player)

    # Aliases mean two different chunks can name one roster row, and a
    # repeat is always a miscount -- a player cannot win one log twice.
    counts = Counter(normalize(ign) for ign in igns)
    repeated = sorted({ign for ign in igns if counts[normalize(ign)] > 1})
    if repeated:
        raise RaffleArgumentError(
            f"{', '.join(repeated)} is named more than once. "
            "Each winner may only be listed once."
        )
    return igns
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_items_raffle.py -q`
Expected: PASS, all of them (the old `split_item_and_*` tests still pass — nothing was removed).

- [ ] **Step 5: Commit**

```bash
git add items_raffle.py tests/test_items_raffle.py
git commit -m "Parse a bare list of winner names for !won"
```

---

### Task 2: `remaining_pool` — subtract this session's winners

**Files:**
- Modify: `items_raffle.py` (add directly after `classify_voters`)
- Test: `tests/test_items_raffle.py`

**Interfaces:**
- Consumes: `attendance_roster.normalize` (already imported in this module as `normalize`)
- Produces: `items_raffle.remaining_pool(eligible: Sequence[str], won: Sequence[str]) -> tuple[list[str], list[str]]` returning `(still_eligible, excluded)`, both in the input's order

- [ ] **Step 1: Write the failing tests**

```python
def test_remaining_pool_removes_this_sessions_winners():
    pool, excluded = items_raffle.remaining_pool(
        ["Jjew", "Kobe", "wile-KAMOTE"], ["Kobe"]
    )

    assert pool == ["Jjew", "wile-KAMOTE"]
    assert excluded == ["Kobe"]


def test_remaining_pool_keeps_the_order_of_the_frozen_list():
    pool, _ = items_raffle.remaining_pool(["Kobe", "Jjew"], [])

    assert pool == ["Kobe", "Jjew"]


def test_remaining_pool_matches_an_alias_not_the_raw_string():
    """A differently-spelled winner must not slip back into a later pool."""
    pool, excluded = items_raffle.remaining_pool(["Jjew", "Kobe"], ["  jjew "])

    assert pool == ["Kobe"]
    assert excluded == ["Jjew"]


def test_remaining_pool_with_no_winners_yet_changes_nothing():
    pool, excluded = items_raffle.remaining_pool(["Jjew", "Kobe"], [])

    assert pool == ["Jjew", "Kobe"]
    assert excluded == []


def test_remaining_pool_can_empty_the_pool():
    pool, excluded = items_raffle.remaining_pool(["Jjew"], ["Jjew"])

    assert pool == []
    assert excluded == ["Jjew"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_raffle.py -k remaining_pool -q`
Expected: FAIL, `AttributeError: module 'items_raffle' has no attribute 'remaining_pool'`

- [ ] **Step 3: Implement `remaining_pool`**

`Sequence` needs importing — the module already imports `Callable` from `collections.abc`, so extend that line to `from collections.abc import Callable, Sequence`.

```python
def remaining_pool(
    eligible: Sequence[str], won: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Split a frozen pool into who may still be drawn and who may not.

    The second list is everyone already holding a win from this session.
    They are returned rather than dropped so the officer can be shown why
    the pool shrank: a pool that quietly gets smaller is how a wrong
    exclusion goes unnoticed.

    Compared through normalize, not as raw strings, because an alias left
    by a roster rename would otherwise let a session winner back in.
    """
    taken = {normalize(name) for name in won}
    pool = [name for name in eligible if normalize(name) not in taken]
    excluded = [name for name in eligible if normalize(name) in taken]
    return pool, excluded
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_items_raffle.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_raffle.py tests/test_items_raffle.py
git commit -m "Subtract a session's winners from a frozen pool"
```

---

### Task 3: `RaffleSession` — the persisted sitting

The session must survive a restart. `State` is stored as JSON inside pinned Discord messages ("shards"); whole-state fields like `raffle_role_ids` live in shard 0 and are picked up by `decode_shards` from the first shard that has them.

There is no separate `winners` field: it is derived from `results`, so the exclusion list and the summary can never disagree.

**Files:**
- Modify: `items_state.py` — add `RaffleSession` after the `Raffle` class; add the `State` field; extend `_encode_with_total`, `decode_state`, `decode_shards`
- Test: `tests/test_items_state.py`

**Interfaces:**
- Produces:
  - `items_state.RaffleSession` (frozen dataclass): `items: tuple[str, ...]`, `position: int`, `results: tuple[tuple[str, tuple[str, ...]], ...]`, `skipped: tuple[str, ...]`
  - properties `current_item -> str | None`, `winners -> tuple[str, ...]`, `finished -> bool`
  - methods `to_dict() -> dict`, `from_dict(raw) -> RaffleSession`
  - `State.raffle_session: RaffleSession | None = None`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_items_state.py` (follow the existing style in that file for building a `State` and round-tripping it through `encode_state` / `decode_shards`):

```python
def _session():
    return items_state.RaffleSession(
        items=("Asta's Heart", "Amentis Foot", "Benji's Heart"),
        position=1,
        results=(("Asta's Heart", ("Kobe",)),),
        skipped=(),
    )


def test_session_reports_the_current_item():
    assert _session().current_item == "Amentis Foot"


def test_a_finished_session_has_no_current_item():
    session = items_state.RaffleSession(items=("Asta's Heart",), position=1)

    assert session.finished is True
    assert session.current_item is None


def test_session_winners_are_flattened_from_the_results():
    session = items_state.RaffleSession(
        items=("A", "B"),
        position=2,
        results=(("A", ("Kobe", "Jjew")), ("B", ("wile-KAMOTE",))),
    )

    assert session.winners == ("Kobe", "Jjew", "wile-KAMOTE")


def test_a_session_survives_a_round_trip_through_the_pin():
    state = items_state.State(officer_channel_id=1, raffle_session=_session())

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.raffle_session == _session()


def test_a_pin_written_before_sessions_existed_loads_as_no_session():
    state = items_state.State(officer_channel_id=1)

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.raffle_session is None


def test_a_session_is_counted_by_fits():
    """A sitting that cannot be persisted must be refused at !startraffle."""
    huge = items_state.RaffleSession(items=tuple(f"Log {n}" * 200 for n in range(400)))
    state = items_state.State(officer_channel_id=1, raffle_session=huge)

    assert items_state.fits(state) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_state.py -k session -q`
Expected: FAIL, `AttributeError: module 'items_state' has no attribute 'RaffleSession'`

- [ ] **Step 3: Implement `RaffleSession`**

Add after the `Raffle` class in `items_state.py`:

```python
@dataclass(frozen=True)
class RaffleSession:
    """One sitting: every closed poll drawn in turn, nobody winning twice.

    `winners` is derived from `results` rather than stored beside it, so
    the list a later pool is filtered against and the list the closing
    summary prints can never drift apart.

    `skipped` is stored rather than inferred from "in items but not in
    results", because a raffle can leave state between the skip and the
    summary -- !poll evicts a drawn raffle to make room -- and a summary
    that silently forgot a log would be worse than one that repeats it.
    """

    items: tuple[str, ...] = ()
    position: int = 0
    results: tuple[tuple[str, tuple[str, ...]], ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def finished(self) -> bool:
        return self.position >= len(self.items)

    @property
    def current_item(self) -> str | None:
        return None if self.finished else self.items[self.position]

    @property
    def winners(self) -> tuple[str, ...]:
        return tuple(ign for _, igns in self.results for ign in igns)

    def to_dict(self) -> dict:
        return {
            "items": list(self.items),
            "position": self.position,
            "results": [
                {"item": item, "winners": list(igns)} for item, igns in self.results
            ],
            "skipped": list(self.skipped),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RaffleSession":
        results = tuple(
            (str(entry["item"]), tuple(str(n) for n in entry.get("winners", [])))
            for entry in raw.get("results", [])
        )
        return cls(
            items=tuple(str(name) for name in raw.get("items", [])),
            position=int(raw.get("position", 0)),
            results=results,
            skipped=tuple(str(name) for name in raw.get("skipped", [])),
        )
```

Add the field to `State`, next to `raffles`:

```python
    # The sitting currently being drawn, or None. Persisted because the
    # free tier restarts: a session held in memory would take the
    # "already won" list with it and quietly re-admit an excluded player.
    raffle_session: "RaffleSession | None" = None
```

In `_encode_with_total`, alongside the other optional shard-0 fields (after the `not_players` block):

```python
    if state.raffle_session is not None:
        first_payload["raffle_session"] = state.raffle_session.to_dict()
```

In `decode_state`, inside the `try`, next to the `raffles` line:

```python
        raffle_session_raw = payload.get("raffle_session")
        raffle_session = (
            RaffleSession.from_dict(raffle_session_raw)
            if isinstance(raffle_session_raw, dict)
            else None
        )
```

and pass `raffle_session=raffle_session` to the `State(...)` it returns.

In `decode_shards`, next to the other whole-state fields:

```python
    raffle_session = next(
        (
            shard.state.raffle_session
            for shard in shards
            if shard.state.raffle_session is not None
        ),
        None,
    )
```

and pass `raffle_session=raffle_session` to the `State(...)` it returns.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_items_state.py -q`
Expected: PASS. Then `python -m pytest tests/ -q` — the whole suite, because `State` gained a field.

- [ ] **Step 5: Commit**

```bash
git add items_state.py tests/test_items_state.py
git commit -m "Persist a raffle session in the pinned state"
```

---

### Task 4: `session_candidates` — which polls make up a sitting

**Files:**
- Modify: `items_state.py` (add after `raffle_item_names`)
- Test: `tests/test_items_state.py`

**Interfaces:**
- Produces: `items_state.session_candidates(state: State, now: str) -> list[Raffle]`

- [ ] **Step 1: Write the failing tests**

```python
def _raffle(item, *, created_at, ends_at="2026-08-09 10:00:00", **kwargs):
    return items_state.Raffle(
        item=item, channel_id=42, message_id=1,
        created_at=created_at, ends_at=ends_at, **kwargs,
    )


NOW = "2026-08-13 12:00:00"


def test_session_candidates_are_closed_and_undrawn_oldest_first():
    state = items_state.State(raffles=[
        _raffle("B", created_at="2026-08-09 11:00:00"),
        _raffle("A", created_at="2026-08-09 10:00:00"),
    ])

    assert [r.item for r in items_state.session_candidates(state, NOW)] == ["A", "B"]


def test_session_candidates_exclude_a_poll_still_open():
    state = items_state.State(raffles=[
        _raffle("A", created_at="2026-08-09 10:00:00", ends_at="2099-01-01 00:00:00"),
    ])

    assert items_state.session_candidates(state, NOW) == []


def test_session_candidates_exclude_a_drawn_raffle():
    state = items_state.State(raffles=[
        _raffle("A", created_at="2026-08-09 10:00:00", winners=("Kobe",), drawn=True),
    ])

    assert items_state.session_candidates(state, NOW) == []


def test_session_candidates_include_a_partly_drawn_raffle():
    """A write that failed part way through still has names to record."""
    state = items_state.State(raffles=[
        _raffle("A", created_at="2026-08-09 10:00:00", winners=("Kobe",), drawn=False),
    ])

    assert [r.item for r in items_state.session_candidates(state, NOW)] == ["A"]


def test_session_candidates_include_a_frozen_but_undrawn_raffle():
    """A poll skipped in an earlier sitting is picked up by the next one."""
    state = items_state.State(raffles=[
        _raffle("A", created_at="2026-08-09 10:00:00", eligible=("Jjew",), listed=True),
    ])

    assert [r.item for r in items_state.session_candidates(state, NOW)] == ["A"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_state.py -k session_candidates -q`
Expected: FAIL, `AttributeError: module 'items_state' has no attribute 'session_candidates'`

- [ ] **Step 3: Implement `session_candidates`**

```python
def session_candidates(state: State, now: str) -> list[Raffle]:
    """The raffles a new sitting would walk through, oldest poll first.

    Closed and not drawn. A partly drawn raffle is included because its
    remaining names still have to be recorded, and a raffle skipped in an
    earlier sitting is included because skipping left it undrawn on
    purpose.

    Ordered by created_at -- the order the polls were opened -- so the
    sitting runs in the order the officer posted them rather than
    whatever order state happens to hold.
    """
    return sorted(
        (r for r in state.raffles if r.ends_at <= now and not r.drawn),
        key=lambda raffle: raffle.created_at,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_items_state.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_state.py tests/test_items_state.py
git commit -m "Choose and order the raffles in a sitting"
```

---

### Task 5: Extract `_freeze_raffle` from `list_cmd`

Pure refactor. `list_cmd` keeps behaving identically and its existing tests are the proof — do not change them. The session will call this helper in Task 7.

**Files:**
- Modify: `items_bot.py:1929-2092` (`list_cmd`)
- Test: `tests/test_items_bot.py` (existing `test_list_*` tests, unchanged)

**Interfaces:**
- Produces: `async items_bot._freeze_raffle(ctx, raffle: items_state.Raffle) -> tuple[items_state.Raffle, items_raffle.VoterSplit] | None`
  - Returns the listed raffle and the split it was built from.
  - Returns `None` after sending its own refusal embed: poll still open, poll unreadable, an unidentified voter, or a pool too large to persist.
  - **Takes `_SHEET_LOCK` itself.** Never call it while holding the lock.

- [ ] **Step 1: Add the helper**

Insert `_freeze_raffle` immediately above `list_cmd`. Its body is `list_cmd`'s lines from the `now = ...` end-time check through the `save_state` call, verbatim — the poll fetch and `poll_is_open` check outside the lock, then everything else inside `async with _SHEET_LOCK`. Keep every existing comment; they explain why each refusal exists.

```python
async def _freeze_raffle(ctx, raffle):
    """Freeze this raffle's eligible pool, or refuse and return None.

    The pool a winner is drawn from must not be able to change between
    the officer looking at it and drawing from it, so this runs once per
    raffle and the result is stored.

    Takes _SHEET_LOCK. asyncio.Lock is not reentrant, so a caller must
    not already hold it.
    """
    item_query = raffle.item
    now = items_rules.format_timestamp(items_rules.now_pht())
    if raffle.ends_at > now:
        await ctx.send(embed=error_embed(
            "Poll still open",
            f"**{raffle.item}** closes at {raffle.ends_at} PHT. "
            "Drawing before then would leave out anyone who has not voted.",
        ))
        return None

    try:
        # The raffle's own channel, not wherever the command was typed.
        # An admin who moves the raffle channel mid-poll must still be
        # able to draw the poll that is sitting in the old one.
        source = bot.get_channel(raffle.channel_id) or ctx.channel
        message = await source.fetch_message(raffle.message_id)
        if poll_is_open(getattr(message, "poll", None)):
            await ctx.send(embed=error_embed(
                "Poll still open",
                f"Discord still has voting open on **{raffle.item}**. "
                "Try again in a moment — freezing now would leave out "
                "anyone who has not voted yet.",
            ))
            return None
        voters = await poll_voters(message)
    except Exception as exc:
        await ctx.send(embed=error_embed(
            "Cannot read the poll",
            f"The poll message for **{raffle.item}** could not be read "
            f"({exc}). Run `!poll {raffle.item}` again to hold a new one.",
        ))
        return None

    async with _SHEET_LOCK:
        # Re-resolved under the lock. The raffle above was found before
        # the poll fetch awaited, so a second officer freezing the same
        # raffle at the same time reaches here holding a Raffle that has
        # already been swapped out of state -- replace_raffle would raise.
        raffle = items_state.find_raffle(_STATE, item_query)
        if raffle is None:
            await ctx.send(embed=error_embed(
                "Nothing to list", f"No raffle for {item_query!r}."
            ))
            return None
        if raffle.listed:
            return raffle, items_raffle.VoterSplit(eligible=list(raffle.eligible))

        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return None

        split = items_raffle.classify_voters(
            voters,
            snapshot.roster,
            holds=lambda ign: items_sheet.holds_special(snapshot, ign, raffle.item),
            identities=items_raffle.Identities(
                bindings=dict(_STATE.bindings),
                not_players=frozenset(_STATE.not_players),
                request_igns=dict(_STATE.igns),
            ),
        )
        if split.unidentified:
            # Freezing now would drop these voters from the pool a winner
            # is drawn from, and nothing later would reveal it happened.
            header = f"{len(split.unidentified)} voter(s) could not be identified:\n\n"
            footer = (
                "\n\nThey must run `!iam <IGN>`, or an officer runs "
                "`!bind @user <IGN>` or `!notaplayer @user`."
            )
            lines = [
                f"<@{voter.user_id}>  nickname {voter.display_name!r}"
                for voter in split.unidentified
            ]
            budget = EMBED_DESCRIPTION_LIMIT - len(header) - len(footer) - 200
            await ctx.send(embed=error_embed(
                "Pool not frozen", header + _capped(lines, budget, "\n") + footer
            ))
            return None

        updated = items_state.replace_raffle(
            _STATE, raffle, eligible=tuple(split.eligible), listed=True
        )
        # A pool too big for one pinned message would make save_state give
        # up -- not just now, but on every later save, silently halting
        # queue persistence. Refuse the freeze instead.
        if not items_state.fits(_STATE):
            items_state.replace_raffle(
                _STATE, updated, eligible=raffle.eligible, listed=raffle.listed
            )
            await ctx.send(embed=error_embed(
                "Entry list too large",
                f"**{raffle.item}** drew {len(split.eligible)} eligible "
                "players — too large for the bot to store safely, so "
                "nothing was frozen. Work the request queue down and try "
                "again; if it still refuses, the raffle needs to be split.",
            ))
            return None

        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

    return updated, split
```

Note the one deliberate wording change: the unidentified-voter footer drops "Then run `!list` again", because `!list` is going away and the retry is automatic. `test_list_refuses_to_freeze_while_a_voter_is_unresolved` asserts only on `<@2>`, the nickname and `!iam`, so it still passes — confirm that when you run the suite.

- [ ] **Step 2: Rewrite `list_cmd` to use it**

```python
@bot.command(name="list")
async def list_cmd(ctx, *, argument: str = ""):
    """Show who is eligible for a closed raffle."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    item_query = argument.strip()
    raffle = items_state.find_raffle(_STATE, item_query) if item_query else None
    if raffle is None:
        await ctx.send(embed=error_embed(
            "Nothing to list",
            f"No raffle for {item_query!r}. Usage: `!list <special log name>`",
        ))
        return

    if raffle.listed:
        # Replayed verbatim, never recomputed: the pool a winner is drawn
        # from must not be able to change between looking and drawing.
        split = items_raffle.VoterSplit(eligible=list(raffle.eligible))
        await ctx.send(embed=ok_embed(
            f"Raffle: {raffle.item}",
            render_pool(raffle.item, split, raffle.winners),
        ))
        return

    frozen = await _freeze_raffle(ctx, raffle)
    if frozen is None:
        return
    updated, split = frozen
    await ctx.send(embed=ok_embed(
        f"Raffle: {updated.item}",
        render_pool(updated.item, split, updated.winners),
    ))
```

- [ ] **Step 3: Run the existing list tests**

Run: `python -m pytest tests/test_items_bot.py -k list -q`
Expected: PASS, unchanged. If `test_two_officers_listing_the_same_raffle_at_once` fails, the "already listed" branch inside the lock is wrong — it must return the raffle so the caller renders the frozen pool, not send its own embed.

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py
git commit -m "Extract the pool freeze from !list"
```

---

### Task 6: Extract `_record_winners` from `winner_cmd`

Pure refactor, same rules as Task 5: `winner_cmd`'s behaviour and its existing tests do not change.

**Files:**
- Modify: `items_bot.py:2095-2286` (`winner_cmd`)
- Test: `tests/test_items_bot.py` (existing `test_winner_*` tests, unchanged)

**Interfaces:**
- Produces:
  - `items_bot.WriteOutcome` (dataclass): `written: list[str]`, `failed: bool`
  - `async items_bot._record_winners(ctx, raffle: items_state.Raffle, chosen: list[str]) -> WriteOutcome`
  - Ticks each name, updates the raffle's `winners` / `drawn`, saves state, and sends the outcome embed itself.
  - **Must be called while already holding `_SHEET_LOCK`** — it does not take it. This is the opposite of `_freeze_raffle`; the difference is deliberate and is why both are documented.

- [ ] **Step 1: Add the helper**

Insert above `winner_cmd`. The body is `winner_cmd`'s lines from `written: list[str] = []` to the final `await ctx.send(embed=embed)`, verbatim, with `updated.item` becoming `raffle.item` where the local name changes.

```python
@dataclasses.dataclass
class WriteOutcome:
    """What a !won attempt actually managed to write."""

    written: list[str]
    failed: bool


async def _record_winners(ctx, raffle, chosen: list[str]) -> WriteOutcome:
    """Tick each winner's checkbox, save, and report what happened.

    The caller must already hold _SHEET_LOCK: this mutates state and
    saves it, and asyncio.Lock is not reentrant.

    `failed` means a write failed hard and names are still to be
    recorded, so the caller must leave the raffle current rather than
    moving on.
    """
    now = items_rules.format_timestamp(items_rules.now_pht())
    written: list[str] = []
    already_ticked: list[str] = []
    ledger_gaps: list[tuple[str, str, list[str]]] = []
    failure: tuple[str, str] | None = None
    not_attempted: list[str] = []

    for position, ign in enumerate(chosen):
        try:
            # ign is bound as a default so that a later refactor firing
            # these concurrently cannot send the last name of the loop to
            # every thread.
            await asyncio.to_thread(
                lambda ign=ign: items_sheet.commit_approval(
                    _SPREADSHEET,
                    ign=ign,
                    item=raffle.item,
                    item_type=items_rules.SPECIAL,
                    timestamp=now,
                    officer=getattr(ctx.author, "display_name", str(ctx.author)),
                    user_id=ctx.author.id,
                    request_id=items_state.new_request_id(),
                )
            )
        except items_sheet.LedgerWriteError as exc:
            # The checkbox IS ticked, so this name is recorded and a
            # retry would skip it -- the ledger row has to be handed
            # over now or it is lost.
            written.append(ign)
            ledger_gaps.append((ign, exc.address, exc.row))
        except items_sheet.AlreadyHeld:
            # A previous run wrote the sheet and then failed to save
            # state. The item HAS been given; say so and move on.
            written.append(ign)
            already_ticked.append(ign)
        except Exception as exc:
            failure = (ign, str(exc))
            not_attempted = chosen[position + 1 :]
            break
        else:
            written.append(ign)

    updated = items_state.replace_raffle(
        _STATE, raffle, winners=(*raffle.winners, *written), drawn=failure is None
    )
    channel = (
        bot.get_channel(_STATE.officer_channel_id)
        if _STATE.officer_channel_id is not None
        else None
    )
    if channel is not None:
        await save_state(channel)

    lines: list[str] = []
    if written:
        label = "Winner" if len(written) == 1 else "Winners"
        lines.append(
            f"🏆 **{label}: {', '.join(written)}** — ticked in "
            f"`{items_sheet.SPECIAL_TAB}`."
        )
    if already_ticked:
        verb = "was" if len(already_ticked) == 1 else "were"
        lines.append(
            f"⚠️ {', '.join(already_ticked)} {verb} already ticked in the "
            "sheet, so nothing was written a second time."
        )
    for ign, address, row in ledger_gaps:
        pasteable = " | ".join(row)
        lines.append(
            f"⚠️ {ign}'s cell {address} is ticked but the "
            f"`{items_sheet.LEDGER_TAB}` row failed. Do NOT re-run for {ign} — "
            f"add this row by hand:\n```\n{pasteable}\n```"
        )
    if failure is not None:
        ign, reason = failure
        remaining = [ign, *not_attempted]
        lines.append(f"❌ **{ign}** was not recorded: {reason}")
        if not_attempted:
            lines.append(f"⏸️ Not attempted: {', '.join(not_attempted)}")
        if not written:
            lines.append("Nothing was written to the sheet.")
        lines.append(
            f"The raffle is still open. Re-run:\n"
            f"`!winner {updated.item} {' - '.join(remaining)}`"
        )
    else:
        lines.append("They are no longer eligible for this log. The raffle is closed.")

    if failure is not None:
        # "Partly" would be a lie when the very first write failed: the
        # sheet is untouched and the officer must not think otherwise.
        outcome = "Partly recorded" if written else "Nothing recorded"
        embed = error_embed(outcome, "\n\n".join(lines))
    else:
        title = "Winner recorded" if len(written) == 1 else "Winners recorded"
        embed = ok_embed(title, "\n\n".join(lines))
    await ctx.send(embed=embed)

    return WriteOutcome(written=written, failed=failure is not None)
```

`items_bot.py` must import `dataclasses` — check the imports at the top and add it if absent.

The `!winner ...` re-run instruction stays as-is for now; Task 8 changes it to `!won` once `!won` exists, so `test_winner_*` tests keep passing through this refactor.

- [ ] **Step 2: Rewrite `winner_cmd` to call it**

Replace everything in `winner_cmd` from `written: list[str] = []` to the end with:

```python
        await _record_winners(ctx, raffle, chosen)
```

Everything above that — the parse, the drawn/open/listed checks, the `recorded_already` / `missing` / `chosen` loop — stays exactly as it is.

- [ ] **Step 3: Run the existing winner tests**

Run: `python -m pytest tests/test_items_bot.py -k winner -q`
Expected: PASS, unchanged.

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py
git commit -m "Extract the winner write from !winner"
```

---

### Task 7: `!startraffle` and the poll-by-poll walk

**Files:**
- Modify: `items_bot.py` — extend `render_pool`; add `_post_current_poll`, `_end_session`, `startraffle_cmd`; add `"startraffle"` to `_RAFFLE_COMMANDS`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `items_state.session_candidates`, `items_state.RaffleSession`, `items_raffle.remaining_pool`, `_freeze_raffle`
- Produces:
  - `render_pool(item, split, winners=(), won_this_session=())` — the fourth parameter is new and defaults to empty, so existing callers are unaffected
  - `async items_bot._post_current_poll(ctx) -> None` — posts the current poll's pool, or ends the session when it is finished. Freezes first if needed; returns silently after `_freeze_raffle` has sent its refusal (the session stays where it is).
  - `async items_bot._end_session(ctx) -> None` — posts the summary and clears `_STATE.raffle_session`
  - `startraffle_cmd`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_items_bot.py` near the other raffle tests. `_open_raffle`, `_configured_raffle`, `_sheet`, `_raffle_ctx` and `_fake_poll_voters` already exist in the file — reuse them, don't redefine.

```python
def test_startraffle_refuses_when_no_poll_has_closed(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, channel = _raffle_ctx()
    _open_raffle(channel)  # ends in 2099, still open

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert "no closed poll" in ctx.sent[-1]["embed"].description.lower()
    assert items_bot._STATE.raffle_session is None


def test_startraffle_walks_the_closed_polls_oldest_first(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, item="Log B", ends="2026-08-09 10:00:00",
        eligible=("Jjew",), listed=True,
    )
    items_bot._STATE.raffles[-1] = dataclasses.replace(
        items_bot._STATE.raffles[-1], created_at="2026-08-09 11:00:00"
    )
    _open_raffle(
        channel, item="Log A", ends="2026-08-09 10:00:00",
        eligible=("Jjew", "Kobe"), listed=True,
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    session = items_bot._STATE.raffle_session
    assert session.items == ("Log A", "Log B")
    assert session.position == 0
    description = ctx.sent[-1]["embed"].description
    assert "Jjew" in description and "Kobe" in description
    assert ctx.sent[-1]["embed"].title.startswith("🎲 Poll 1 of 2")


def test_startraffle_freezes_a_pool_that_has_not_been_listed(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"), holds=("Kobe",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "Kobe")]),
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is True
    assert raffle.eligible == ("Jjew",)


def test_startraffle_holds_on_an_unidentified_voter(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "xXshadowXx")]),
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert ctx.sent[-1]["embed"].title == "❌ Pool not frozen"
    session = items_bot._STATE.raffle_session
    assert session is not None and session.position == 0
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is False


def test_startraffle_during_a_session_retries_the_current_poll(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00", eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=0
    )

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert items_bot._STATE.raffle_session.items == ("Asta's Heart",)
    assert "Jjew" in ctx.sent[-1]["embed"].description


def test_a_session_with_one_poll_ends_after_it_is_drawn(monkeypatch):
    """Reaching the end posts the summary and clears the session."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Asta's Heart",), position=1,
        results=(("Asta's Heart", ("Kobe",)),),
    )

    asyncio.run(items_bot._post_current_poll(ctx))

    assert items_bot._STATE.raffle_session is None
    description = ctx.sent[-1]["embed"].description
    assert "Asta's Heart" in description and "Kobe" in description


def test_the_summary_marks_a_skipped_log(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=2,
        results=(("Log A", ("Kobe",)),), skipped=("Log B",),
    )

    asyncio.run(items_bot._post_current_poll(ctx))

    description = ctx.sent[-1]["embed"].description
    assert "Log B" in description and "skipped" in description.lower()


def test_render_pool_names_who_won_earlier_in_the_session():
    split = items_raffle.VoterSplit(eligible=["Jjew"])

    description = items_bot.render_pool(
        "Asta's Heart", split, won_this_session=["Kobe"]
    )

    assert "Kobe" in description
    assert "Won earlier this session" in description
```

`dataclasses` must be imported in the test module — add it if absent.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_bot.py -k "startraffle or session or won_this_session" -q`
Expected: FAIL, `AttributeError: module 'items_bot' has no attribute 'startraffle_cmd'`

- [ ] **Step 3: Extend `render_pool`**

Change the signature and add the group after the `already_have` block, before `from_request`:

```python
def render_pool(
    item: str,
    split: items_raffle.VoterSplit,
    winners: tuple[str, ...] = (),
    won_this_session: Sequence[str] = (),
) -> str:
```

and inside, after the `already_have` block:

```python
    if won_this_session:
        block = "🏆 **Won earlier this session** (excluded)"
        lines += ["", block, _capped(list(won_this_session), max(budget, 0), ", ")]
        budget -= len(lines[-1]) + len(block)
```

`Sequence` needs importing in `items_bot.py` — the module already imports `Callable` from `collections.abc`; extend that line.

- [ ] **Step 4: Add the session walk**

```python
async def _end_session(ctx) -> None:
    """Post the summary of the whole sitting and clear it from state."""
    session = _STATE.raffle_session
    lines: list[str] = []
    won = {item: igns for item, igns in session.results}
    for item in session.items:
        if item in won:
            igns = won[item]
            label = "Winner" if len(igns) == 1 else "Winners"
            lines.append(f"🏆 **{item}** — {label}: {', '.join(igns)}")
        elif item in session.skipped:
            lines.append(f"⏭️ **{item}** — skipped, still undrawn")
        else:
            # Reached only if a raffle left state mid-sitting. Say so
            # rather than printing a log with no outcome at all.
            lines.append(f"❔ **{item}** — no outcome recorded")

    _STATE.raffle_session = None
    channel = (
        bot.get_channel(_STATE.officer_channel_id)
        if _STATE.officer_channel_id is not None
        else None
    )
    if channel is not None:
        await save_state(channel)

    await ctx.send(embed=ok_embed(
        "Raffle session finished",
        "\n".join(lines) + "\n\nRun `!startraffle` again to draw any log left undrawn.",
    ))


async def _post_current_poll(ctx) -> None:
    """Show the pool for the session's current poll, or finish the sitting.

    Returns silently when the freeze refused: _freeze_raffle has already
    said why, and the session deliberately stays on this poll so the
    officer can fix the cause and have it retried.

    Must NOT be called while holding _SHEET_LOCK -- _freeze_raffle takes it.
    """
    session = _STATE.raffle_session
    if session is None:
        return
    if session.finished:
        await _end_session(ctx)
        return

    item = session.current_item
    raffle = items_state.find_raffle(_STATE, item)
    if raffle is None or raffle.drawn:
        # The raffle was superseded or drawn from outside the session.
        # Skip past it rather than stalling on a poll that no longer exists.
        _STATE.raffle_session = dataclasses.replace(
            session, position=session.position + 1, skipped=(*session.skipped, item)
        )
        await ctx.send(embed=warn_embed(
            "Poll gone",
            f"**{item}** is no longer waiting to be drawn, so it was passed over.",
        ))
        await _post_current_poll(ctx)
        return

    if raffle.listed:
        split = items_raffle.VoterSplit(eligible=list(raffle.eligible))
    else:
        frozen = await _freeze_raffle(ctx, raffle)
        if frozen is None:
            return
        raffle, split = frozen

    pool, excluded = items_raffle.remaining_pool(raffle.eligible, session.winners)
    body = render_pool(
        raffle.item,
        dataclasses.replace(split, eligible=pool),
        raffle.winners,
        won_this_session=excluded,
    )
    footer = (
        "\n\nDraw the winner yourself, then run `!won <IGN>`. "
        "`!skipraffle` leaves this log undrawn."
        if pool
        else "\n\nNobody is left eligible for this log. Run `!skipraffle` to move on."
    )
    await ctx.send(embed=ok_embed(
        f"🎲 Poll {session.position + 1} of {len(session.items)} — {raffle.item}",
        body + footer,
    ))


@bot.command(name="startraffle")
async def startraffle_cmd(ctx):
    """Begin a raffle session, or retry the poll the current one is stuck on."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    if _STATE.raffle_session is not None:
        # A session already running means this is the manual retry for a
        # poll whose freeze refused, not a request for a second sitting.
        await _post_current_poll(ctx)
        return

    now = items_rules.format_timestamp(items_rules.now_pht())
    candidates = items_state.session_candidates(_STATE, now)
    if not candidates:
        await ctx.send(embed=error_embed(
            "Nothing to draw",
            "There is no closed poll waiting for a winner. Open one with "
            "`!poll <special log>` and run this again once it closes.",
        ))
        return

    previous = _STATE.raffle_session
    _STATE.raffle_session = items_state.RaffleSession(
        items=tuple(raffle.item for raffle in candidates)
    )
    if not items_state.fits(_STATE):
        _STATE.raffle_session = previous
        await ctx.send(embed=error_embed(
            "Session not started",
            "The bot's storage is full, so this sitting could not be saved. "
            "Work the request queue down and try again.",
        ))
        return

    channel = (
        bot.get_channel(_STATE.officer_channel_id)
        if _STATE.officer_channel_id is not None
        else None
    )
    if channel is not None:
        await save_state(channel)

    names = ", ".join(raffle.item for raffle in candidates)
    await ctx.send(embed=ok_embed(
        f"🎲 Raffle session started — {len(candidates)} poll(s)",
        f"{names}\n\nEach pool is shown in turn. A player who wins may not "
        "win again in this session.",
    ))
    await _post_current_poll(ctx)
```

Add `"startraffle"` to `_RAFFLE_COMMANDS`.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_items_bot.py -q`
Expected: PASS, including `test_every_registered_command_is_classified`.

- [ ] **Step 6: Run the whole suite and commit**

```bash
python -m pytest tests/ -q
git add items_bot.py tests/test_items_bot.py
git commit -m "Walk every closed poll in a raffle session"
```

---

### Task 8: `!won` — record the current poll's winners

**Files:**
- Modify: `items_bot.py` — add `won_cmd`; add `"won"` to `_RAFFLE_COMMANDS`; change the re-run hint in `_record_winners` from `!winner` to `!won`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `items_raffle.split_igns`, `items_raffle.remaining_pool`, `_record_winners`, `_post_current_poll`
- Produces: `won_cmd`

The lock discipline here is the part to get right: `_record_winners` must run **inside** `_SHEET_LOCK`, and `_post_current_poll` must run **after the `async with` block exits**, because `_freeze_raffle` takes the same lock.

- [ ] **Step 1: Write the failing tests**

```python
def test_won_refuses_with_no_session(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()
    monkeypatch.setattr(items_sheet, "commit_approval", lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))

    assert "!startraffle" in ctx.sent[-1]["embed"].description


def test_won_ticks_the_checkbox_and_advances_the_session(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    calls = {}
    monkeypatch.setattr(items_sheet, "commit_approval",
                        lambda s, **kw: calls.update(kw) or "C4")

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert calls["ign"] == "Kobe"
    assert calls["item"] == "Log A"
    session = items_bot._STATE.raffle_session
    assert session.position == 1
    assert session.results == (("Log A", ("Kobe",)),)
    assert items_state.find_raffle(items_bot._STATE, "Log A").drawn is True


def test_a_winner_is_excluded_from_every_later_pool(monkeypatch):
    """The whole point: one win per player per session."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )
    monkeypatch.setattr(items_sheet, "commit_approval", lambda s, **kw: "C4")

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    description = ctx.sent[-1]["embed"].description
    assert "Log B" in ctx.sent[-1]["embed"].title
    assert "1. Jjew" in description
    assert "Won earlier this session" in description
    # Kobe must not be offered again -- he appears only in the excluded group.
    assert "2. Kobe" not in description


def test_won_refuses_a_player_excluded_by_an_earlier_win(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=1,
        results=(("Log A", ("Kobe",)),),
    )
    monkeypatch.setattr(items_sheet, "commit_approval",
                        lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert "already won" in ctx.sent[-1]["embed"].description
    assert items_bot._STATE.raffle_session.position == 1


def test_won_refuses_a_player_not_in_the_pool(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(items=("Log A",))
    monkeypatch.setattr(items_sheet, "commit_approval",
                        lambda *a, **k: pytest.fail("wrote"))

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert "not on the eligible list" in ctx.sent[-1]["embed"].description
    assert items_bot._STATE.raffle_session.position == 0


def test_won_records_several_winners_for_one_log(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(items=("Log A",))
    written = []
    monkeypatch.setattr(items_sheet, "commit_approval",
                        lambda s, **kw: written.append(kw["ign"]) or "C4")

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew - Kobe"))

    assert written == ["Jjew", "Kobe"]
    assert items_bot._STATE.raffle_session is None  # only poll, session ended


def test_a_failed_write_leaves_the_poll_current(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew", "Kobe"), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )

    def _boom(spreadsheet, **kwargs):
        raise RuntimeError("Google said no")

    monkeypatch.setattr(items_sheet, "commit_approval", _boom)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    session = items_bot._STATE.raffle_session
    assert session.position == 0, "a failed write must not move the session on"
    assert session.results == ()
    assert items_state.find_raffle(items_bot._STATE, "Log A").drawn is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_bot.py -k won_ -q`
Expected: FAIL, `AttributeError: module 'items_bot' has no attribute 'won_cmd'`

- [ ] **Step 3: Implement `won_cmd`**

```python
@bot.command(name="won")
async def won_cmd(ctx, *, argument: str = ""):
    """Record the winners of the raffle session's current poll."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    session = _STATE.raffle_session
    if session is None or session.finished:
        await ctx.send(embed=error_embed(
            "No raffle session",
            "No raffle session is running, and a winner can only be recorded "
            "inside one. Run `!startraffle` first.",
        ))
        return

    async with _SHEET_LOCK:
        raffle = items_state.find_raffle(_STATE, session.current_item)
        if raffle is None:
            await ctx.send(embed=error_embed(
                "Winner refused",
                f"**{session.current_item}** is no longer in the bot's state. "
                "Run `!startraffle` to move the session on.",
            ))
            return
        if not raffle.listed:
            await ctx.send(embed=error_embed(
                "Winner refused",
                f"The pool for **{raffle.item}** has not been frozen yet — "
                "the session is waiting on something. Fix what it reported, "
                "or run `!startraffle` to retry it.",
            ))
            return

        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        try:
            igns = items_raffle.split_igns(argument, snapshot.roster)
        except items_raffle.RaffleArgumentError as exc:
            await ctx.send(embed=error_embed("Winner refused", str(exc)))
            return

        pool, excluded = items_raffle.remaining_pool(raffle.eligible, session.winners)

        # Every name is checked before the first write, so a typo in the
        # third name cannot leave the first two ticked.
        blocked: list[str] = []
        recorded_already: list[str] = []
        missing: list[str] = []
        chosen: list[str] = []
        for ign in igns:
            wanted = items_rules.normalize(ign)
            if any(items_rules.normalize(w) == wanted for w in excluded):
                blocked.append(ign)
            elif any(items_rules.normalize(w) == wanted for w in raffle.winners):
                recorded_already.append(ign)
            elif any(items_rules.normalize(n) == wanted for n in pool):
                chosen.append(next(n for n in pool if items_rules.normalize(n) == wanted))
            else:
                missing.append(ign)

        if blocked:
            await ctx.send(embed=error_embed(
                "Winner refused",
                f"{', '.join(blocked)} already won earlier in this session and "
                "may not win again. Nothing was recorded.",
            ))
            return
        if missing:
            suggestions = get_close_matches(missing[0], list(pool), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            await ctx.send(embed=error_embed(
                "Winner refused",
                f"{', '.join(missing)} — not on the eligible list for "
                f"**{raffle.item}**.{hint} Nothing was recorded.",
            ))
            return
        if recorded_already:
            await ctx.send(embed=error_embed(
                "Winner refused",
                f"**{', '.join(recorded_already)}** already won **{raffle.item}** "
                "— nothing was written a second time. Re-run naming only the "
                "players still to record.",
            ))
            return

        outcome = await _record_winners(ctx, raffle, chosen)
        if outcome.failed:
            # Names are still to be recorded for this log. Leaving the
            # session here is what lets the officer re-run !won with the
            # rest instead of the sitting moving past an unfinished draw.
            return

        _STATE.raffle_session = dataclasses.replace(
            session,
            position=session.position + 1,
            results=(*session.results, (raffle.item, tuple(outcome.written))),
        )
        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

    # Outside the lock: _freeze_raffle takes it, and asyncio.Lock is not
    # reentrant -- calling this inside the block above would deadlock.
    await _post_current_poll(ctx)
```

Add `"won"` to `_RAFFLE_COMMANDS`.

In `_record_winners`, change the re-run hint now that `!won` exists:

```python
        lines.append(
            f"The raffle is still open. Re-run:\n"
            f"`!won {' - '.join(remaining)}`"
        )
```

This changes what `test_winner_*` tests see. Update any that assert on the `!winner <item>` re-run text — grep for `"Re-run"` in `tests/test_items_bot.py`. Those tests are deleted in Task 11 anyway, so the minimum fix is fine.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_items_bot.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite and commit**

```bash
python -m pytest tests/ -q
git add items_bot.py tests/test_items_bot.py
git commit -m "Record a session poll's winners with !won"
```

---

### Task 9: `!skipraffle`

**Files:**
- Modify: `items_bot.py` — add `skipraffle_cmd`; add `"skipraffle"` to `_RAFFLE_COMMANDS`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `_post_current_poll`
- Produces: `skipraffle_cmd`

- [ ] **Step 1: Write the failing tests**

```python
def test_skipraffle_leaves_the_log_undrawn_and_moves_on(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    _open_raffle(channel, item="Log B", ends="2026-08-09 10:00:00",
                 eligible=("Kobe",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(
        items=("Log A", "Log B"), position=0
    )

    asyncio.run(items_bot.skipraffle_cmd.callback(ctx))

    session = items_bot._STATE.raffle_session
    assert session.position == 1
    assert session.skipped == ("Log A",)
    assert items_state.find_raffle(items_bot._STATE, "Log A").drawn is False
    assert "Log B" in ctx.sent[-1]["embed"].title


def test_a_skipped_log_is_picked_up_by_the_next_session(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, item="Log A", ends="2026-08-09 10:00:00",
                 eligible=("Jjew",), listed=True)
    items_bot._STATE.raffle_session = items_state.RaffleSession(items=("Log A",))

    asyncio.run(items_bot.skipraffle_cmd.callback(ctx))
    assert items_bot._STATE.raffle_session is None

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert items_bot._STATE.raffle_session.items == ("Log A",)


def test_skipraffle_refuses_with_no_session(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch)
    ctx, _ = _raffle_ctx()

    asyncio.run(items_bot.skipraffle_cmd.callback(ctx))

    assert "!startraffle" in ctx.sent[-1]["embed"].description
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_bot.py -k skipraffle -q`
Expected: FAIL, `AttributeError: module 'items_bot' has no attribute 'skipraffle_cmd'`

- [ ] **Step 3: Implement `skipraffle_cmd`**

```python
@bot.command(name="skipraffle")
async def skipraffle_cmd(ctx):
    """Leave the session's current poll undrawn and move to the next."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    session = _STATE.raffle_session
    if session is None or session.finished:
        await ctx.send(embed=error_embed(
            "No raffle session",
            "No raffle session is running. Run `!startraffle` first.",
        ))
        return

    item = session.current_item
    _STATE.raffle_session = dataclasses.replace(
        session, position=session.position + 1, skipped=(*session.skipped, item)
    )
    channel = (
        bot.get_channel(_STATE.officer_channel_id)
        if _STATE.officer_channel_id is not None
        else None
    )
    if channel is not None:
        await save_state(channel)

    await ctx.send(embed=warn_embed(
        "Poll skipped",
        f"**{item}** was left undrawn. It stays in the bot's state and the "
        "next `!startraffle` will offer it again.",
    ))
    await _post_current_poll(ctx)
```

Add `"skipraffle"` to `_RAFFLE_COMMANDS`.

- [ ] **Step 4: Run the tests, then the whole suite**

Run: `python -m pytest tests/test_items_bot.py -q` then `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Pass over a poll with !skipraffle"
```

---

### Task 10: Auto-retry the stuck poll from `!iam`, `!bind` and `!notaplayer`

A session is stuck exactly when it is active, not finished, and its current raffle is not `listed` — the pool is posted only after a successful freeze. That is read from state directly, so there is no separate "blocked" flag to keep in sync.

**Files:**
- Modify: `items_bot.py` — add `_retry_blocked_session`; call it at the end of `iam_cmd`, `bind_cmd`, `notaplayer_cmd` (all three currently end after `_save_binding_change` and a `ctx.send`)
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Produces: `async items_bot._retry_blocked_session(ctx) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_binding_the_unidentified_voter_retries_the_stuck_poll(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "xXshadowXx")]),
    )
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))
    assert ctx.sent[-1]["embed"].title == "❌ Pool not frozen"

    member = FakeMember(member_id=2)
    asyncio.run(items_bot.bind_cmd.callback(ctx, member, argument="Kobe"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is True, "the fix should have retried the freeze"
    assert "Asta's Heart" in ctx.sent[-1]["embed"].title


def test_a_binding_with_no_session_running_retries_nothing(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, _ = _raffle_ctx()
    member = FakeMember(member_id=2)

    asyncio.run(items_bot.bind_cmd.callback(ctx, member, argument="Kobe"))

    assert items_bot._STATE.raffle_session is None
    assert "🎲" not in str(ctx.sent[-1]["embed"].title)


def test_notaplayer_also_retries_the_stuck_poll(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "xXshadowXx")]),
    )
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    asyncio.run(items_bot.notaplayer_cmd.callback(ctx, FakeMember(member_id=2)))

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is True
```

Check `FakeMember`'s constructor in the test module before writing these — use whatever keyword it actually takes for the id, and match how the existing `bind_cmd` / `notaplayer_cmd` tests build a member.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_bot.py -k "retries or retry" -q`
Expected: FAIL — the raffle is still unlisted after the bind.

- [ ] **Step 3: Implement the retry**

```python
async def _retry_blocked_session(ctx) -> None:
    """Re-attempt the poll a session is held on, after an identity is fixed.

    A session is held exactly when its current raffle is not listed: the
    pool is posted only after a successful freeze, so an unlisted current
    raffle means the freeze refused. Read from state rather than tracked
    in a flag, which could drift out of sync with the raffle it describes.

    A retry that still finds an unidentified voter simply refuses again.
    It never advances the session and never writes to the sheet, so a
    failed retry costs nothing.
    """
    session = _STATE.raffle_session
    if session is None or session.finished:
        return
    raffle = items_state.find_raffle(_STATE, session.current_item)
    if raffle is not None and raffle.listed:
        return
    await _post_current_poll(ctx)
```

Call it as the last line of `iam_cmd`, `bind_cmd` and `notaplayer_cmd`, after their existing success `ctx.send`. Add it only on the success path — a command that refused changed no identity and has nothing to retry.

- [ ] **Step 4: Run the tests, then the whole suite**

Run: `python -m pytest tests/test_items_bot.py -q` then `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Retry a held poll when the identity is fixed"
```

---

### Task 11: Delete `!list`, `!winner` and the parsing they needed

Everything the session replaces goes now, in one commit, so the tree is never in a state where two ways to draw a raffle both work.

**Files:**
- Modify: `items_raffle.py` — delete `split_item_and_ign`, `split_item_and_igns`, `WINNER_USAGE`
- Modify: `items_bot.py` — delete `list_cmd`, `winner_cmd`; update `_RAFFLE_COMMANDS`, `itemhelp_cmd`, `poll_cmd`'s messages, `items_state.evict_for_new_raffle`'s docstring
- Modify: `tests/test_items_raffle.py` — delete the `split_item_and_*` tests
- Modify: `tests/test_items_bot.py` — delete the `test_list_*` and `test_winner_*` tests

- [ ] **Step 1: Delete the commands and the dead parsing**

```bash
grep -n "split_item_and_ign\|WINNER_USAGE\|list_cmd\|winner_cmd" items_raffle.py items_bot.py tests/*.py
```

Delete `list_cmd` and `winner_cmd` from `items_bot.py`, and `split_item_and_ign`, `split_item_and_igns` and `WINNER_USAGE` from `items_raffle.py`. Keep `WINNER_SPLIT` and `DANGLING_SEPARATOR` — `split_igns` uses both. Keep `render_pool`, `_capped` and `_winner_footer` — the session uses them.

Remove `"list"` and `"winner"` from `_RAFFLE_COMMANDS`; it should end up as:

```python
_RAFFLE_COMMANDS = frozenset({
    "poll", "cancelpoll", "startraffle", "won", "skipraffle",
    "iam", "bind", "notaplayer",
})
```

- [ ] **Step 2: Delete the tests for the deleted code**

Delete every `test_list_*` and `test_winner_*` function from `tests/test_items_bot.py`, and the `split_item_and_*` tests from `tests/test_items_raffle.py`.

Three of the deleted bot tests cover behaviour the session still has, and their coverage must not be lost. Rewrite these against the session instead of deleting them outright:

- `test_list_reads_the_poll_from_the_raffles_own_channel` → drive it through `startraffle_cmd` with the raffle's `channel_id` pointing at a different registered channel from `ctx.channel`.
- `test_list_defers_to_discords_own_expiry_not_the_stored_one` → same, asserting `!startraffle` refuses to freeze while Discord still has the poll open.
- `test_list_refuses_to_freeze_a_pool_it_could_never_save` → same, asserting the "Entry list too large" refusal and that the raffle is left unlisted.

- [ ] **Step 3: Update the user-facing text**

In `itemhelp_cmd` (`items_bot.py:1528-1556`), replace the raffle command list:

```python
            "**`!poll <special log> [--hours N]`** — open a poll "
            f"({items_raffle.DEFAULT_POLL_HOURS}h by default)\n"
            "**`!startraffle`** — draw every closed poll, one at a time\n"
            "**`!won <IGN>`** — record the current poll's winner\n"
            "**`!won <IGN> - <IGN>`** — several winners for one log\n"
            "**`!skipraffle`** — leave the current poll undrawn\n"
            "**`!cancelpoll <special log>`** — cancel an open poll\n"
```

and update the member-facing paragraph above it so it says a player may win only once per session.

In `poll_cmd`, three messages name the old commands:
- the unfinished-draw refusal: "Finish it with `!winner`" → "Finish it with `!startraffle` and `!won`"
- the all-slots-full refusal: "Run `!winner` on one of them first" → "Draw one with `!startraffle` first"
- the success embed: "Run `!list {item}` after that." → "Run `!startraffle` after it closes."

In `items_state.evict_for_new_raffle` and `raffle_to_evict`, the docstrings say "the frozen eligible pool that `!winner` checks against" and "unreachable by `!list` and `!winner`". Update those names to `!won` / `!startraffle`. Same in `poll_cmd`'s inline comments.

Then sweep for anything missed:

```bash
grep -rn '!list\|!winner' items_bot.py items_raffle.py items_state.py
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: PASS. `test_every_registered_command_is_classified` proves the command set and `_RAFFLE_COMMANDS` still agree. `test_help_says_request_is_gear_only_and_lists_the_raffle_commands` will need its assertions updated to the new command names.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check .
git add items_bot.py items_raffle.py items_state.py tests/
git commit -m "Retire !list and !winner in favour of the session"
```

---

### Task 12: Documentation

**Files:**
- Modify: `README.md:278-315` (the raffle command table and the prose under it)
- Modify: `docs/item-bot-setup.md:44,234-258`

- [ ] **Step 1: Rewrite the README raffle section**

Replace the `!list` / `!winner` rows in the command table with:

| Command | What it does |
|---|---|
| `!startraffle` | Draw every closed poll in one sitting, oldest first. Shows each pool, waits for the winner, then moves on. |
| `!won <IGN>` | Record the winner of the poll currently on screen and tick their checkbox in the Special Logs tab. |
| `!won <IGN> - <IGN>` | Several winners for one log, for a log that dropped more than once. Names are split on a hyphen with a space on both sides, so a hyphenated IGN stays intact. |
| `!skipraffle` | Leave the current poll undrawn and move to the next. The next `!startraffle` offers it again. |

Then rewrite the prose to cover, in order: a player may win only once per session; the pool for each poll excludes anyone already holding that log *and* anyone who won earlier in the session; the draw is still done by hand; a winner can only be recorded inside a session; an unidentified voter holds the session on that poll and fixing it with `!iam` / `!bind` / `!notaplayer` retries automatically.

- [ ] **Step 2: Rewrite the setup doc**

Update the command cheat sheet at `docs/item-bot-setup.md:234-238` to the five raffle commands, and the prose at 244-258 the same way. Line 44 mentions `!list` refusing — reword to the session holding.

- [ ] **Step 3: Verify no stale references remain**

```bash
grep -rn '!list\|!winner' README.md docs/ --include='*.md' | grep -v docs/superpowers
```
Expected: no output. (`docs/superpowers/` holds historical plans and specs, which are a record of what was decided at the time and are not rewritten.)

- [ ] **Step 4: Run the whole suite once more and commit**

```bash
python -m pytest tests/ -q
python -m ruff check .
git add README.md docs/item-bot-setup.md
git commit -m "Document the raffle session"
```

---

## Manual verification after Task 12

The tests use fakes, so before this is trusted in production, run one real sitting in Discord:

1. `!poll Log A --hours 1` and `!poll Log B --hours 1`, vote on both from two accounts, wait for them to close.
2. `!startraffle` — confirm both logs are listed in the opening message and Log A's pool appears.
3. `!won <a player in both pools>` — confirm the checkbox is ticked in the Special Logs tab, and that Log B's pool then appears **without** that player and with them named under "Won earlier this session".
4. `!skipraffle` on Log B — confirm the summary marks it skipped, and `!startraffle` offers Log B again.
5. Restart the bot mid-session (between two polls) and confirm `!won` still works and still excludes the earlier winner — this is the one thing the fakes cannot prove.
