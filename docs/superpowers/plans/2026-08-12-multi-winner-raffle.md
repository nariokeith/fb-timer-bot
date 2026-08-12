# Multi-winner Raffle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one special log poll produce several winners, drawn with a single
`!winner Amentis Foot A - B - C` command.

**Architecture:** Four tasks. Task 1 turns the single `winner` string in the
raffle record into a `winners` tuple plus a `drawn` flag, with no behaviour
change — a mechanical migration that leaves every existing test green. Task 2
adds a pure parser for the multi-name argument. Task 3 wires the parser into
`!winner` and replaces the single sheet write with a loop that survives a
partial failure. Task 4 updates the docs.

**Tech Stack:** Python 3.14, discord.py, gspread, pytest. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-12-multi-winner-raffle-design.md`. Read
  it before starting.
- Run tests with `python -m pytest` from the repo root. Nothing in this plan
  needs network, a Discord token, or a Google API key — every sheet and Discord
  object in these tests is a fake already present in `tests/test_items_bot.py`.
- Bot state lives in a **pinned Discord message** holding JSON. Raffles written
  under the old `{"winner": "Jjew"}` shape are already in production, so
  `Raffle.from_dict` must keep reading them forever.
- Winners are separated by `\s+-\s+` — a hyphen with whitespace on **both**
  sides. A bare hyphen must never split, because `wile-KAMOTE` is a real roster
  name.
- No `--winners N` flag is added to `!poll`. `!winner` accepts any number of
  names.
- Comment style in this repo explains *why*, never *what*. Match it. Do not add
  comments that restate the code.

---

### Task 1: Plural winner schema, no behaviour change

Rename `Raffle.winner: str` to `winners: tuple[str, ...]`, add `drawn: bool`,
and update every reader. After this task `!winner` still records exactly one
name and the whole suite still passes.

`drawn` is a separate field rather than `bool(winners)` because Task 3
introduces a state where `winners` is non-empty but the draw is unfinished.
This is the same reasoning that already justifies `listed` (see the `Raffle`
docstring).

**Files:**
- Modify: `items_state.py` — `Raffle` dataclass, `to_dict`, `from_dict`,
  `raffle_to_evict`
- Modify: `items_bot.py` — `poll_cmd`, `render_pool`, `list_cmd`, `winner_cmd`,
  `cancelpoll_cmd`
- Test: `tests/test_items_state.py`, `tests/test_items_bot.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `items_state.Raffle(..., winners: tuple[str, ...] = (), drawn: bool = False)`;
  `items_bot.render_pool(item: str, split: items_raffle.VoterSplit, winners: tuple[str, ...] = ()) -> str`;
  `items_bot._winner_footer(winners: tuple[str, ...]) -> str`.

- [ ] **Step 1: Write the failing migration and eviction tests**

Add to `tests/test_items_state.py`. The existing helper `_raffle(...)` in that
file builds a `Raffle`; these tests pass the new kwargs to it.

```python
def test_a_raffle_saved_under_the_old_single_winner_key_still_loads():
    """State written before multi-winner is sitting in the pinned message."""
    legacy = {
        "item": "Asta's Heart",
        "channel_id": 42,
        "message_id": 999,
        "created_at": "2026-08-09 01:00:00",
        "ends_at": "2026-08-09 10:00:00",
        "eligible": ["Jjew", "Kobe"],
        "listed": True,
        "winner": "Jjew",
    }

    raffle = items_state.Raffle.from_dict(legacy)

    assert raffle.winners == ("Jjew",)
    assert raffle.drawn is True


def test_a_legacy_raffle_with_no_winner_loads_as_undrawn():
    legacy = {
        "item": "Asta's Heart",
        "channel_id": 42,
        "message_id": 999,
        "created_at": "2026-08-09 01:00:00",
        "ends_at": "2026-08-09 10:00:00",
        "winner": "",
    }

    raffle = items_state.Raffle.from_dict(legacy)

    assert raffle.winners == ()
    assert raffle.drawn is False


def test_several_winners_survive_a_round_trip():
    raffle = _raffle(winners=("Jjew", "Kobe"), drawn=True)

    restored = items_state.Raffle.from_dict(raffle.to_dict())

    assert restored.winners == ("Jjew", "Kobe")
    assert restored.drawn is True


def test_eviction_will_not_drop_a_partly_drawn_raffle():
    """Its ticked checkboxes and unfinished draw are only recorded here.

    The oldest raffle carrying winners is the partly drawn one, so a
    filter on `winners` rather than `drawn` would pick it.
    """
    # raffle_to_evict returns early unless every slot is taken, so the
    # list is padded to capacity with undrawn fillers.
    raffles = [
        _raffle(item="Partly drawn", created="2026-08-01 10:00:00",
                winners=("Kobe",), drawn=False),
        _raffle(item="Fully drawn", created="2026-08-02 10:00:00",
                winners=("Jjew",), drawn=True),
    ]
    raffles += [
        _raffle(item=f"Log {n}", created=f"2026-08-03 {n:02d}:00:00")
        for n in range(items_state.MAX_RAFFLES - 2)
    ]
    state = items_state.State(raffles=raffles)

    allowed, victim = items_state.raffle_to_evict(state)

    assert allowed is True
    assert victim.item == "Fully drawn"
```

`_raffle` is defined at `tests/test_items_state.py:267` as
`_raffle(item="Asta's Heart", created="2026-08-09 10:00:00", ends="2026-08-10 10:00:00", **kwargs)`
— it fills in `channel_id` and `message_id` and passes everything else straight
to `Raffle`, so `winners=` and `drawn=` work without touching it.

- [ ] **Step 1b: Confirm the pinned message still fits with winner lists**

`items_state.fits` bounds the state against Discord's message size, and winner
lists add bytes to every raffle. Add this beside the existing worst-case
capacity test at `tests/test_items_state.py:415`, mirroring its shape (20
raffles × 35 eligible names, 30 queued requests) with winners populated:

```python
def test_capacity_holds_when_every_raffle_has_several_winners():
    """Winner lists add bytes to the pinned state; capacity must survive."""
    state = items_state.State(
        raffles=[
            _raffle(
                item=f"Special Log Number {n}",
                created=f"2026-08-09 {n:02d}:00:00",
                listed=True,
                eligible=tuple(f"PlayerName{i:02d}" for i in range(35)),
                winners=("PlayerName01", "PlayerName02", "PlayerName03"),
                drawn=True,
            )
            for n in range(20)
        ],
        queue=[
            items_state.PendingRequest(
                f"id{i:03d}", i, f"Player {i}", "Asta's Belt", "Gear",
                "2026-08-09 09:00:00",
            )
            for i in range(30)
        ],
    )

    assert items_state.fits(state)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_state.py -k "legacy or several_winners or partly_drawn" -v`
Expected: FAIL — `Raffle.__init__() got an unexpected keyword argument 'winners'`.

- [ ] **Step 3: Change the dataclass in `items_state.py`**

Replace the `winner: str = ""` field and both serialisers:

```python
@dataclass(frozen=True)
class Raffle:
    """One special log poll and everything decided from it.

    `listed` cannot be inferred from `eligible`: a raffle where nobody
    was eligible is a real outcome, and it must stay distinguishable
    from one that has not been listed yet -- otherwise !winner would
    tell an officer to run !list again forever.

    `drawn` cannot be inferred from `winners` for the same shape of
    reason. A !winner command whose sheet write failed part way through
    leaves some names recorded and the draw unfinished, and that must
    stay distinguishable from a draw that completed.
    """

    item: str
    channel_id: int
    message_id: int
    created_at: str
    ends_at: str
    eligible: tuple[str, ...] = ()
    listed: bool = False
    winners: tuple[str, ...] = ()
    drawn: bool = False

    def to_dict(self) -> dict:
        return {
            "item": self.item,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "created_at": self.created_at,
            "ends_at": self.ends_at,
            "eligible": list(self.eligible),
            "listed": self.listed,
            "winners": list(self.winners),
            "drawn": self.drawn,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Raffle":
        # Raffles pinned before multi-winner carry a single "winner"
        # string. One recorded winner meant the draw was over, so the
        # migrated raffle is drawn.
        if "winners" in raw:
            winners = tuple(str(name) for name in raw["winners"])
        else:
            legacy = str(raw.get("winner", ""))
            winners = (legacy,) if legacy else ()
        return cls(
            item=str(raw["item"]),
            channel_id=int(raw["channel_id"]),
            message_id=int(raw["message_id"]),
            created_at=str(raw["created_at"]),
            ends_at=str(raw["ends_at"]),
            eligible=tuple(str(name) for name in raw.get("eligible", [])),
            listed=bool(raw.get("listed", False)),
            winners=winners,
            drawn=bool(raw.get("drawn", bool(winners))),
        )
```

In `raffle_to_evict`, change the drawn filter so only a completed draw can be
evicted:

```python
    drawn = [raffle for raffle in state.raffles if raffle.drawn]
```

Extend that function's docstring with one sentence: a partly drawn raffle is
also unevictable, because its remaining names still have to be recorded.

- [ ] **Step 4: Update every reader in `items_bot.py`**

These are mechanical. Find each with
`grep -n "\.winner\b\|winner=" items_bot.py`.

In `poll_cmd`, both places that ask "has this raffle been drawn" become
`winners` checks — any recorded winner protects the raffle from being refused
against or superseded:

```python
        if existing is not None and existing.ends_at > now_text and not existing.winners:
```

```python
        superseded = existing if existing is not None and not existing.winners else None
```

Add a `_winner_footer` helper just above `render_pool`:

```python
def _winner_footer(winners: tuple[str, ...]) -> str:
    if not winners:
        return ""
    label = "Winner" if len(winners) == 1 else "Winners"
    return f"🏆 **{label}: {', '.join(winners)}**"
```

Change `render_pool` to take the tuple. Only the signature, the two footer
lines, and the two `if winner:` guards change; the budget arithmetic in between
stays exactly as it is, because it already subtracts `len(footer)`:

```python
def render_pool(
    item: str, split: items_raffle.VoterSplit, winners: tuple[str, ...] = ()
) -> str:
    ...
    header = f"**Eligible for {item}** ({len(split.eligible)})"
    trophy = _winner_footer(winners)
    footer = f"\n\n{trophy}" if trophy else ""
    budget = EMBED_DESCRIPTION_LIMIT - len(header) - len(footer) - 200
    ...
    if trophy:
        lines += ["", trophy]
    return "\n".join(lines)
```

Both `render_pool` call sites in `list_cmd` pass `raffle.winners` and
`updated.winners` instead of `raffle.winner` / `updated.winner`.

In `winner_cmd`, the already-drawn guard reads the flag, and its message joins
the names:

```python
        if raffle.drawn:
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"**{raffle.item}** has already been drawn: "
                    f"**{', '.join(raffle.winners)}** won it.",
                )
            )
            return
```

All three `items_state.replace_raffle(_STATE, raffle, winner=on_list)` calls in
`winner_cmd` become:

```python
            items_state.replace_raffle(
                _STATE, raffle, winners=(on_list,), drawn=True
            )
```

In `cancelpoll_cmd`, the refusal reads `winners` — a partly drawn raffle has
ticked checkboxes behind it and must not be cancellable either:

```python
        if raffle.winners:
            await ctx.send(
                embed=error_embed(
                    "Cancel refused",
                    f"**{raffle.item}** has already been drawn: "
                    f"**{', '.join(raffle.winners)}** won it. A drawn raffle "
                    "is distribution history.",
                )
            )
            return
```

- [ ] **Step 5: Update the existing tests to the new field names**

In `tests/test_items_bot.py` and `tests/test_items_state.py`, replace every
`winner="X"` keyword with `winners=("X",), drawn=True`, and every
`.winner == "X"` assertion with `.winners == ("X",)`. Every `.winner == ""`
assertion becomes `.winners == ()`.

Rename the `drawn=` parameter of the `_fill_every_raffle_slot` helper in
`tests/test_items_bot.py` to `already_drawn=` so it no longer reads like the new
field, and update its body and its two callers:

```python
def _fill_every_raffle_slot(monkeypatch, ends="2099-01-01 00:00:00", already_drawn=()):
    logs = [f"Log {n}" for n in range(items_state.MAX_RAFFLES)]
    _sheet(monkeypatch, special=("Player Name", "Asta's Heart", *logs))
    items_bot._STATE.raffles = [
        items_state.Raffle(
            item=name, channel_id=42, message_id=n,
            created_at=f"2026-08-09 {n:02d}:00:00", ends_at=ends,
            winners=("Kobe",) if name in already_drawn else (),
            drawn=name in already_drawn,
        )
        for n, name in enumerate(logs)
    ]
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS. If anything fails on an attribute error, it is a reader you
missed — `grep -rn "\.winner\b" items_bot.py items_state.py items_board.py tests/`
finds it.

- [ ] **Step 7: Commit**

```bash
git add items_state.py items_bot.py tests/test_items_state.py tests/test_items_bot.py
git commit -m "Store raffle winners as a list with a drawn flag"
```

---

### Task 2: Parse several IGNs out of the `!winner` argument

A pure function in `items_raffle.py`. Not wired into the command yet, so the
suite stays green either way.

**Files:**
- Modify: `items_raffle.py` — add `split_item_and_igns`, update `WINNER_USAGE`
- Test: `tests/test_items_raffle.py`

**Interfaces:**
- Consumes: existing `items_raffle.split_item_and_ign(argument, item_names, roster) -> tuple[str, str]` (unchanged), `items_rules.resolve_ign`, `attendance_roster.normalize`.
- Produces: `items_raffle.split_item_and_igns(argument: str, item_names: list[str], roster: list[str]) -> tuple[str, list[str]]` — the item name, then the resolved roster spellings in the order typed. Raises `items_raffle.RaffleArgumentError`.

- [ ] **Step 1: Write the failing parser tests**

Append to `tests/test_items_raffle.py`. `ROSTER` is already defined at the top
of that file as `["Jjew", "Kobe", "Ryuu", "chinchong ni Mumu", "wile-KAMOTE"]`.

```python
ITEMS = ["Asta's Heart", "Amentis Foot"]


def test_one_winner_still_parses_with_no_dash():
    assert items_raffle.split_item_and_igns("Asta's Heart Jjew", ITEMS, ROSTER) == (
        "Asta's Heart",
        ["Jjew"],
    )


def test_the_item_may_run_into_the_first_winner():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot Jjew - Kobe - Ryuu", ITEMS, ROSTER
    ) == ("Amentis Foot", ["Jjew", "Kobe", "Ryuu"])


def test_the_item_may_be_followed_by_its_own_dash():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot - Jjew - Kobe", ITEMS, ROSTER
    ) == ("Amentis Foot", ["Jjew", "Kobe"])


def test_a_hyphenated_ign_is_not_split_into_two_winners():
    """'wile-KAMOTE' has no space around its hyphen, so it is one name."""
    assert items_raffle.split_item_and_igns(
        "Amentis Foot wile-KAMOTE - Kobe", ITEMS, ROSTER
    ) == ("Amentis Foot", ["wile-KAMOTE", "Kobe"])


def test_a_multi_word_ign_survives_the_split():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot chinchong ni Mumu - Kobe", ITEMS, ROSTER
    ) == ("Amentis Foot", ["chinchong ni Mumu", "Kobe"])


def test_extra_spaces_around_the_dash_are_tolerated():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot Jjew   -   Kobe", ITEMS, ROSTER
    ) == ("Amentis Foot", ["Jjew", "Kobe"])


def test_an_alias_resolves_in_a_later_position():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot Jjew - KobePH", ITEMS, ROSTER
    ) == ("Amentis Foot", ["Jjew", "Kobe"])


def test_the_same_player_twice_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="more than once"):
        items_raffle.split_item_and_igns("Amentis Foot Jjew - Jjew", ITEMS, ROSTER)


def test_an_alias_colliding_with_a_real_name_counts_as_a_duplicate():
    with pytest.raises(items_raffle.RaffleArgumentError, match="more than once"):
        items_raffle.split_item_and_igns("Amentis Foot Kobe - KobePH", ITEMS, ROSTER)


def test_a_trailing_dash_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="empty"):
        items_raffle.split_item_and_igns("Amentis Foot Jjew - ", ITEMS, ROSTER)


def test_an_unknown_name_in_a_later_position_is_refused_by_name():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Nobody"):
        items_raffle.split_item_and_igns("Amentis Foot Jjew - Nobody", ITEMS, ROSTER)


def test_an_item_with_no_winner_at_all_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Which player"):
        items_raffle.split_item_and_igns("Amentis Foot", ITEMS, ROSTER)
```

`test_an_alias_colliding_with_a_real_name_counts_as_a_duplicate` relies on
`KobePH` resolving to `Kobe`, which the existing test
`test_an_alias_resolves_through_the_roster` already proves for this roster.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_raffle.py -k split_item_and_igns -v`
Expected: FAIL — `module 'items_raffle' has no attribute 'split_item_and_igns'`.
(If the `-k` filter matches nothing, run the file without it; the names above do
not all contain the function name.)

Run instead: `python -m pytest tests/test_items_raffle.py -v`
Expected: the twelve new tests ERROR on the missing attribute; everything else
passes.

- [ ] **Step 3: Implement the parser**

Add these imports at the top of `items_raffle.py`, alongside the existing ones:

```python
import re
from collections import Counter
```

Replace `WINNER_USAGE` and add the new function after `split_item_and_ign`:

```python
WINNER_USAGE = (
    "Usage: `!winner <special log name> <IGN>`, or "
    "`!winner <special log name> <IGN> - <IGN> - <IGN>` for several winners."
)

# A hyphen only separates winners when it has whitespace on BOTH sides.
# 'wile-KAMOTE' is a roster row, so a bare hyphen cannot be the delimiter.
WINNER_SPLIT = re.compile(r"\s+-\s+")


def split_item_and_igns(
    argument: str, item_names: list[str], roster: list[str]
) -> tuple[str, list[str]]:
    """Split '<item> <IGN> - <IGN> - ...' into the item and its winners.

    The item may run into the first name or be followed by its own dash;
    officers type both. The two readings cannot collide, because the
    first is only taken when the leading chunk is EXACTLY a raffle name.

    Every name is resolved before this returns, so a typo is refused
    before any checkbox is ticked rather than half way through.
    """
    chunks = WINNER_SPLIT.split(argument.strip())
    if any(not chunk.strip() for chunk in chunks):
        raise RaffleArgumentError(
            f"There is an empty name between two dashes. {WINNER_USAGE}"
        )
    chunks = [chunk.strip() for chunk in chunks]

    index = {normalize(name): name for name in item_names}
    head = index.get(normalize(chunks[0]))
    if head is not None and len(chunks) == 1:
        raise RaffleArgumentError(f"Which player won **{head}**? {WINNER_USAGE}")

    if head is not None:
        item, igns = head, []
    else:
        item, first = split_item_and_ign(chunks[0], item_names, roster)
        igns = [first]

    for chunk in chunks[1:]:
        try:
            player = items_rules.resolve_ign(chunk, roster)
        except items_rules.RequestParseError as exc:
            raise RaffleArgumentError(str(exc)) from None
        if player is None:
            suggestions = get_close_matches(chunk, roster, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise RaffleArgumentError(
                f"No player named {chunk!r} in the sheet.{hint} {WINNER_USAGE}"
            )
        igns.append(player)

    # Aliases mean two different chunks can name one roster row, and a
    # repeat is always a miscount -- a player cannot win the same log
    # twice.
    counts = Counter(normalize(ign) for ign in igns)
    repeated = sorted({ign for ign in igns if counts[normalize(ign)] > 1})
    if repeated:
        raise RaffleArgumentError(
            f"{', '.join(repeated)} is named more than once. "
            "Each winner may only be listed once."
        )

    return item, igns
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_items_raffle.py -v`
Expected: PASS, all tests including the pre-existing ones. The existing
`test_winner_refuses_a_single_word_argument` matches on `"Usage"`, which the new
`WINNER_USAGE` still contains.

- [ ] **Step 5: Commit**

```bash
git add items_raffle.py tests/test_items_raffle.py
git commit -m "Parse several winners out of a !winner argument"
```

---

### Task 3: Record several winners, and survive a partial failure

**Files:**
- Modify: `items_bot.py` — `winner_cmd` only
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `items_raffle.split_item_and_igns` (Task 2); `items_state.Raffle.winners` / `.drawn` (Task 1); existing `items_sheet.commit_approval(spreadsheet, *, ign, item, item_type, timestamp, officer, user_id, request_id) -> str`, which raises `items_sheet.AlreadyHeld` or `items_sheet.LedgerWriteError(address, row, cause)`.
- Produces: no new public names.

- [ ] **Step 1: Write the failing command tests**

Append to `tests/test_items_bot.py`. `_configured_raffle`, `_sheet`,
`_raffle_ctx` and `_open_raffle` are existing helpers in that file; `_sheet`
takes a `roster=` tuple.

```python
def test_winner_records_three_names_from_one_command(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00",
        eligible=("Jjew", "Kobe", "Ryuu"), listed=True,
    )
    written = []
    monkeypatch.setattr(
        items_sheet, "commit_approval",
        lambda spreadsheet, **kw: (written.append(kw["ign"]), "C4")[1],
    )

    asyncio.run(
        items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew - Kobe - Ryuu")
    )

    assert written == ["Jjew", "Kobe", "Ryuu"]
    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew", "Kobe", "Ryuu")
    assert raffle.drawn is True
    assert ctx.sent[-1]["embed"].title == "✅ Winners recorded"


def test_a_failure_part_way_keeps_the_raffle_open_for_a_retry(monkeypatch):
    """The first name is already ticked; the rest must stay drawable."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00",
        eligible=("Jjew", "Kobe", "Ryuu"), listed=True,
    )

    def _fail_on_kobe(spreadsheet, **kw):
        if kw["ign"] == "Kobe":
            raise RuntimeError("Sheets is down")
        return "C4"

    monkeypatch.setattr(items_sheet, "commit_approval", _fail_on_kobe)

    asyncio.run(
        items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew - Kobe - Ryuu")
    )

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew",)
    assert raffle.drawn is False
    description = ctx.sent[-1]["embed"].description
    assert "Sheets is down" in description
    assert "Ryuu" in description, "the un-attempted name must be named"
    assert "!winner Asta's Heart Kobe - Ryuu" in description


def test_the_retry_after_a_partial_failure_completes_the_draw(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe", "Ryuu"),
        listed=True, winners=("Jjew",), drawn=False,
    )
    monkeypatch.setattr(items_sheet, "commit_approval", lambda spreadsheet, **kw: "C4")

    asyncio.run(
        items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Kobe - Ryuu")
    )

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew", "Kobe", "Ryuu")
    assert raffle.drawn is True


def test_retyping_an_already_recorded_winner_writes_nothing(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe", "Ryuu"),
        listed=True, winners=("Jjew",), drawn=False,
    )
    monkeypatch.setattr(
        items_sheet, "commit_approval",
        lambda *a, **k: pytest.fail("wrote a second time"),
    )

    asyncio.run(
        items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew - Kobe")
    )

    description = ctx.sent[-1]["embed"].description
    assert "Jjew" in description
    assert "already" in description.casefold()
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winners == ("Jjew",)


def test_one_bad_name_refuses_the_whole_command(monkeypatch):
    """Nothing is ticked, so the officer can fix the typo and re-run as typed."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe", "Ryuu"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe"), listed=True
    )
    monkeypatch.setattr(
        items_sheet, "commit_approval", lambda *a, **k: pytest.fail("wrote")
    )

    asyncio.run(
        items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew - Ryuu")
    )

    assert "not on the eligible list" in ctx.sent[-1]["embed"].description
    assert "Ryuu" in ctx.sent[-1]["embed"].description
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").winners == ()


def test_a_ledger_failure_on_one_name_does_not_stop_the_others(monkeypatch):
    """Its checkbox IS ticked, so a retry would skip it and lose the row."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, channel = _raffle_ctx()
    _open_raffle(
        channel, ends="2026-08-09 10:00:00", eligible=("Jjew", "Kobe"), listed=True
    )
    row = ["2026-08-09 12:00:00", "Jjew", "Asta's Heart", "Special", "Keith", "1", "abc"]

    def _ledger_failure(spreadsheet, **kw):
        if kw["ign"] == "Jjew":
            raise items_sheet.LedgerWriteError("C4", row, RuntimeError("append failed"))
        return "C5"

    monkeypatch.setattr(items_sheet, "commit_approval", _ledger_failure)

    asyncio.run(items_bot.winner_cmd.callback(ctx, argument="Asta's Heart Jjew - Kobe"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.winners == ("Jjew", "Kobe")
    assert raffle.drawn is True
    assert "C4" in ctx.sent[-1]["embed"].description


def test_render_pool_labels_several_winners(monkeypatch):
    split = items_raffle.VoterSplit(eligible=["Jjew", "Kobe"])

    text = items_bot.render_pool("Asta's Heart", split, ("Jjew", "Kobe"))

    assert "Winners: Jjew, Kobe" in text
    assert "Winner: Jjew" in items_bot.render_pool("Asta's Heart", split, ("Jjew",))
    assert "Winner" not in items_bot.render_pool("Asta's Heart", split)
```

The `footer` local in `render_pool` is only ever used for the budget
subtraction; the trophy line itself is appended once via `lines`. Keep it that
way — the singular/plural label must not be rendered twice.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_items_bot.py -k "three_names or part_way or retry_after or retyping or bad_name or ledger_failure_on_one or several_winners" -v`
Expected: FAIL — the multi-name argument is rejected by the current
single-winner parser with "No open raffle matches that name".

- [ ] **Step 3: Rewrite the body of `winner_cmd`**

Replace everything from the `split_item_and_ign` call to the end of the function
with the following. The guards before it (`_refuse_raffle`, `read_snapshot`) are
unchanged.

```python
        try:
            item, igns = items_raffle.split_item_and_igns(
                argument, items_state.raffle_item_names(_STATE), snapshot.roster
            )
        except items_raffle.RaffleArgumentError as exc:
            await ctx.send(embed=error_embed("Winner refused", str(exc)))
            return

        raffle = items_state.find_raffle(_STATE, item)
        now = items_rules.format_timestamp(items_rules.now_pht())

        if raffle.drawn:
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"**{raffle.item}** has already been drawn: "
                    f"**{', '.join(raffle.winners)}** won it.",
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

        # Every name is checked before the first write, so a typo in the
        # third name cannot leave the first two ticked.
        recorded_already: list[str] = []
        missing: list[str] = []
        chosen: list[str] = []
        for ign in igns:
            wanted = items_rules.normalize(ign)
            if any(items_rules.normalize(w) == wanted for w in raffle.winners):
                recorded_already.append(ign)
                continue
            on_list = next(
                (n for n in raffle.eligible if items_rules.normalize(n) == wanted),
                None,
            )
            (chosen if on_list is not None else missing).append(on_list or ign)

        if missing:
            suggestions = get_close_matches(
                missing[0], list(raffle.eligible), n=3, cutoff=0.6
            )
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"{', '.join(missing)} — not on the eligible list for "
                    f"**{raffle.item}**.{hint} Nothing was recorded.",
                )
            )
            return
        if recorded_already:
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"**{', '.join(recorded_already)}** already won "
                    f"**{raffle.item}** — nothing was written a second time. "
                    f"Re-run naming only the players still to record.",
                )
            )
            return

        written: list[str] = []
        already_ticked: list[str] = []
        ledger_gaps: list[tuple[str, str, list[str]]] = []
        failure: tuple[str, str] | None = None
        not_attempted: list[str] = []

        for position, ign in enumerate(chosen):
            try:
                # ign is bound as a default: a bare closure would send
                # the LAST name of the loop to every thread.
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
            _STATE,
            raffle,
            winners=(*raffle.winners, *written),
            drawn=failure is None,
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
        lines.append(
            f"⚠️ {', '.join(already_ticked)} was already ticked in the sheet, "
            "so nothing was written a second time."
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
        lines.append(
            f"The raffle is still open. Re-run:\n"
            f"`!winner {updated.item} {' - '.join(remaining)}`"
        )
    else:
        lines.append(
            "They are no longer eligible for this log. The raffle is closed."
        )

    title = "Winners recorded" if len(written) != 1 else "Winner recorded"
    await ctx.send(
        embed=(
            error_embed("Partly recorded", "\n\n".join(lines))
            if failure is not None
            else ok_embed(title, "\n\n".join(lines))
        )
    )
```

Note the report is built **outside** `async with _SHEET_LOCK`, matching the
existing function, so the local variables above must be assigned before the
`with` block exits on every path — they are, because every early return sends
its own embed and returns.

- [ ] **Step 4: Run the new tests**

Run: `python -m pytest tests/test_items_bot.py -k "three_names or part_way or retry_after or retyping or bad_name or ledger_failure_on_one or several_winners" -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and fix the older winner tests**

Run: `python -m pytest -q`

Two pre-existing tests assert message wording that this task changes:
`test_winner_ticks_the_checkbox_and_closes_the_raffle` expects the title
`"✅ Winner recorded"` (still correct for one name — leave it), and
`test_a_ledger_failure_closes_the_raffle_and_hands_over_the_row` expects the
title `"❌ Winner recorded, ledger not"`, which no longer exists. Update that
test to assert on the description instead: `"C4" in description` and
`"Jjew" in description`, with `raffle.winners == ("Jjew",)` and
`raffle.drawn is True`. Do not weaken any assertion that checks *what was
written to the sheet*.

Expected after fixes: PASS.

- [ ] **Step 6: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Draw several winners from one !winner command"
```

---

### Task 4: Document the new syntax

**Files:**
- Modify: `items_bot.py` — the `!winner` line in `itemhelp_cmd`
- Modify: `README.md:281` and the surrounding raffle section
- Modify: `docs/item-bot-setup.md:237` and the paragraph at `:243`
- Test: `tests/test_items_bot.py` — existing
  `test_help_says_request_is_gear_only_and_lists_the_raffle_commands`

**Interfaces:**
- Consumes: the syntax from Task 2.
- Produces: nothing.

- [ ] **Step 1: Update the help embed**

In `itemhelp_cmd`, replace the `!winner` line:

```python
            "**`!winner <special log> <IGN>`** — record the draw\n"
            "**`!winner <log> <IGN> - <IGN> - <IGN>`** — several winners "
            "at once\n"
```

- [ ] **Step 2: Update `README.md`**

Replace the `!winner` table row and add one below it:

```markdown
| `!winner <special log> <IGN>` | Record the winner and tick their checkbox in the Special Logs tab. |
| `!winner <special log> <IGN> - <IGN>` | Record several winners from one poll, for a log that dropped more than once. Names are split on a hyphen with a space on both sides, so a hyphenated IGN stays intact. |
```

In the paragraph at `README.md:289`, add: if a sheet write fails part way, the
raffle stays open and the bot prints the exact command to re-run for the names
it did not record.

- [ ] **Step 3: Update `docs/item-bot-setup.md`**

Replace the `!winner` line in the command block at `:237`:

```
!winner <special log> <IGN>              records the draw, ticks their checkbox
!winner <special log> <IGN> - <IGN>      records several winners from one poll
```

Extend the paragraph at `:245`, which already lists what `!winner` refuses, with
the new refusals: a name repeated within one command, and a name already
recorded for that raffle.

- [ ] **Step 4: Run the help test**

Run: `python -m pytest tests/test_items_bot.py -k help -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite one last time**

Run: `python -m pytest -q`
Expected: PASS, no skips introduced by this work.

- [ ] **Step 6: Commit**

```bash
git add items_bot.py README.md docs/item-bot-setup.md
git commit -m "Document multi-winner draws"
```
