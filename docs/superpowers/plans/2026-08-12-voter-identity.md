# Voter Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `!list` resolve every poll voter to a roster row, and refuse to
freeze the raffle pool while any voter is unresolved.

**Architecture:** Five tasks. Task 1 adds two state fields and their
shard encode/decode with no behaviour change. Task 2 adds the pure
resolution ladder in `items_raffle`. Task 3 adds the three binding commands.
Task 4 wires the ladder into `!list` and makes an unresolved voter block the
freeze. Task 5 documents it.

**Tech Stack:** Python 3.14, discord.py, gspread, pytest. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-12-voter-identity-design.md`. Read it
  before starting.
- Run tests with `./.venv/bin/python -m pytest` from the repo root. A bare
  `python`/`pytest` is NOT on PATH — `.venv` is the only interpreter with
  pytest. Baseline on `main`: **600 passed, 0 failures**.
- Nothing here needs network, a Discord token, or a Google API key. Every
  sheet and Discord object in the tests is a fake already in `tests/`.
- Bot state is JSON in a **pinned Discord message**. Pins written before this
  change must keep loading, and `to_dict`/`decode_state` must stay readable by
  an older bot (it ignores unknown keys, so only additions are safe — never
  rename or remove an existing key).
- IGNs resolve through `items_rules.resolve_ign` only — exact normalized match
  or `attendance_roster.ALIASES`. **Never fuzzy.** A wrong match credits an
  item to the wrong player permanently.
- Comment style in this repo explains *why*, never *what*. Match it. Do not
  add comments that restate the code.
- Every command that grows state calls `items_state.fits(_STATE)` before
  saving and rolls the change back when it returns False, exactly as
  `request_cmd` does (`items_bot.py:701-713`).

---

### Task 1: State fields for bindings and not-a-player marks

Adds `bindings` and `not_players` to `State`, with shard encode, decode, and
cross-shard merge. No behaviour change: nothing reads them yet.

**Files:**
- Modify: `items_state.py` — `State`, `_encode_with_total`, `decode_state`,
  `decode_shards`
- Test: `tests/test_items_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `items_state.State(..., bindings: dict[str, str] = {}, not_players: list[str] = [])`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_items_state.py`. The existing `_raffle(...)` helper is at
line 267; `items_state.State` is constructed directly elsewhere in the file.

```python
def test_bindings_and_not_players_survive_a_round_trip():
    state = items_state.State(
        officer_channel_id=1,
        bindings={"111": "Jjew", "222": "Kobe"},
        not_players=["333", "444"],
    )

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.bindings == {"111": "Jjew", "222": "Kobe"}
    assert restored.not_players == ["333", "444"]


def test_a_pin_written_before_bindings_existed_loads_with_them_empty():
    """Production pins predate this field; they must not fail to load."""
    old = items_state.State(officer_channel_id=1, igns={"111": "Jjew"})

    restored = items_state.decode_shards(items_state.encode_state(old))

    assert restored.bindings == {}
    assert restored.not_players == []
    assert restored.igns == {"111": "Jjew"}


def test_bindings_spill_across_shards_and_all_survive():
    """One shard cannot hold hundreds of bindings; none may be dropped."""
    state = items_state.State(
        officer_channel_id=1,
        bindings={str(10**17 + i): f"PlayerName{i:03d}" for i in range(300)},
    )

    contents = items_state.encode_state(state)
    restored = items_state.decode_shards(contents)

    assert len(contents) > 1, "300 bindings must not fit one shard"
    assert restored.bindings == state.bindings


def test_three_hundred_bindings_still_fit_the_pinned_message():
    """Measured at design time: ~38 bytes each, 18 of 20 shards at 300."""
    state = items_state.State(
        officer_channel_id=1,
        bindings={str(10**17 + i): f"PlayerName{i:03d}" for i in range(300)},
        raffles=[
            _raffle(
                item=f"Special Log Number {n}",
                created=f"2026-08-09 {n:02d}:00:00",
                listed=True,
                eligible=tuple(f"PlayerName{i:02d}" for i in range(40)),
            )
            for n in range(20)
        ],
    )

    assert items_state.fits(state)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_items_state.py -k "bindings or not_players" -v`
Expected: FAIL — `State.__init__() got an unexpected keyword argument 'bindings'`.

- [ ] **Step 3: Add the fields to `State`**

In `items_state.py`, in the `State` dataclass, add these two fields after the
existing `igns` field and its comment:

```python
    # Discord id -> the IGN this account IS. Set deliberately by !iam or
    # !bind, unlike `igns` above, which only records what the account last
    # requested under and may name an alt.
    bindings: dict[str, str] = field(default_factory=dict)
    # Discord ids known to have no roster row at all -- guests and former
    # members. Without this they would block every raffle freeze forever.
    not_players: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Encode them into the shards**

In `_encode_with_total`, add `"bindings": {}` to `first_payload` beside
`"igns": {}`. Then add this spill loop immediately AFTER the existing
`for user_id, ign in state.igns.items():` loop and before the
`for request in state.queue:` loop:

```python
    for user_id, ign in state.bindings.items():
        current = payloads[-1]
        current.setdefault("bindings", {})[user_id] = ign
        if len(_render(current)) <= MAX_CONTENT:
            continue

        current["bindings"].pop(user_id)
        current = {"part": len(payloads), "total": total, "bindings": {}, "queue": []}
        payloads.append(current)
        current["bindings"][user_id] = ign
        if len(_render(current)) > MAX_CONTENT:
            raise ValueError("a binding is too large for a state shard")
```

`not_players` is a short list of id strings, so it rides in shard 0 with the
other whole-state configuration. Add this beside the existing
`if state.raffle_role_ids:` block in `first_payload`:

```python
    if state.not_players:
        first_payload["not_players"] = list(state.not_players)
```

The existing final loop that checks every rendered shard against
`MAX_CONTENT` already catches a shard 0 made too large by this.

- [ ] **Step 5: Decode them**

In `decode_state`, beside the existing `igns = ...` line, add:

```python
        bindings = {
            str(k): str(v) for k, v in dict(payload.get("bindings", {})).items()
        }
        not_players = [str(u) for u in payload.get("not_players", [])]
```

and pass `bindings=bindings, not_players=not_players` into the `State(...)`
constructed in the returned `Shard`.

In `decode_shards`, beside the existing `igns: dict[str, str] = {}`, add:

```python
    bindings: dict[str, str] = {}
    not_players: list[str] = []
```

Inside the existing `for shard in shards:` loop, beside `igns.update(...)`:

```python
        bindings.update(shard.state.bindings)
        for user_id in shard.state.not_players:
            # Shard 0 carries the list, but a re-sharded pin can repeat it.
            if user_id not in not_players:
                not_players.append(user_id)
```

and pass `bindings=bindings, not_players=not_players` into the returned
`State(...)`.

- [ ] **Step 6: Run the tests**

Run: `./.venv/bin/python -m pytest tests/test_items_state.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: 600 + 4 new tests, 0 failures.

- [ ] **Step 8: Commit**

```bash
git add items_state.py tests/test_items_state.py
git commit -m "Store deliberate voter bindings and not-a-player marks"
```

---

### Task 2: The resolution ladder

Pure logic in `items_raffle`. `classify_voters` gains one parameter carrying
the three identity sources.

**Files:**
- Modify: `items_raffle.py` — add `Identities`, extend `VoterSplit`, rewrite
  `classify_voters`, add `resolve_identity`
- Modify: `items_bot.py` — the one `classify_voters` call site in `list_cmd`,
  minimally, to keep the suite green
- Test: `tests/test_items_raffle.py`

**Interfaces:**
- Consumes: `items_state.State.bindings` / `.not_players` (Task 1), the
  existing `items_state.State.igns`.
- Produces:
  - `items_raffle.Identities(bindings: dict[str, str], not_players: frozenset[str], request_igns: dict[str, str])`
  - `items_raffle.resolve_identity(voter: Voter, roster: list[str], identities: Identities) -> tuple[str | None, str | None]` — the IGN and which rung matched (`"binding"`, `"nickname"`, `"request"`, `"skipped"`), or `(None, None)`
  - `items_raffle.classify_voters(voters, roster, holds, identities) -> VoterSplit`
  - `VoterSplit(eligible, already_have, unidentified, from_request, skipped)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_raffle.py`. `ROSTER` is already defined at the top
of that file as `["Jjew", "Kobe", "Ryuu", "chinchong ni Mumu", "wile-KAMOTE"]`.

```python
def _identities(bindings=None, not_players=(), request_igns=None):
    return items_raffle.Identities(
        bindings=bindings or {},
        not_players=frozenset(not_players),
        request_igns=request_igns or {},
    )


def _voter(user_id=1, display_name="xXshadowXx"):
    return items_raffle.Voter(user_id=user_id, display_name=display_name)


def test_a_binding_resolves_a_nickname_nothing_else_could():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(bindings={"7": "Kobe"})
    )

    assert (ign, source) == ("Kobe", "binding")


def test_a_binding_beats_a_nickname_that_would_resolve_differently():
    """This is how an officer corrects a wrong nickname match."""
    ign, source = items_raffle.resolve_identity(
        _voter(7, "BK | Jjew"), ROSTER, _identities(bindings={"7": "Kobe"})
    )

    assert (ign, source) == ("Kobe", "binding")


def test_a_not_a_player_voter_is_skipped():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(not_players=["7"])
    )

    assert (ign, source) == (None, "skipped")


def test_a_nickname_still_resolves_when_nothing_is_bound():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "BK | Jjew"), ROSTER, _identities()
    )

    assert (ign, source) == ("Jjew", "nickname")


def test_the_last_request_ign_is_the_fallback():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(request_igns={"7": "Ryuu"})
    )

    assert (ign, source) == ("Ryuu", "request")


def test_a_nickname_beats_the_request_fallback():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "BK | Jjew"), ROSTER, _identities(request_igns={"7": "Ryuu"})
    )

    assert (ign, source) == ("Jjew", "nickname")


def test_a_binding_naming_a_row_no_longer_in_the_roster_is_unresolved():
    """A player removed from the sheet must not stay drawable."""
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(bindings={"7": "LeftTheGuild"})
    )

    assert (ign, source) == (None, None)


def test_a_request_fallback_naming_a_missing_row_is_unresolved():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(request_igns={"7": "LeftTheGuild"})
    )

    assert (ign, source) == (None, None)


def test_an_unresolvable_voter_is_unidentified():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities()
    )

    assert (ign, source) == (None, None)


def test_classify_splits_every_group():
    voters = [
        _voter(1, "BK | Jjew"),          # nickname
        _voter(2, "xXshadowXx"),         # binding -> Kobe
        _voter(3, "who even"),           # request -> Ryuu
        _voter(4, "a guest"),            # skipped
        _voter(5, "nobody at all"),      # unidentified
    ]
    identities = _identities(
        bindings={"2": "Kobe"}, not_players=["4"], request_igns={"3": "Ryuu"}
    )

    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: ign == "Ryuu", identities=identities
    )

    assert split.eligible == ["Jjew", "Kobe"]
    assert split.already_have == ["Ryuu"]
    assert [v.user_id for v in split.skipped] == [4]
    assert [v.user_id for v in split.unidentified] == [5]
    assert [(v.user_id, ign) for v, ign in split.from_request] == [(3, "Ryuu")]


def test_a_duplicate_across_two_accounts_is_still_collapsed():
    """One player voting from an alt account must not double their odds."""
    voters = [_voter(1, "BK | Jjew"), _voter(2, "xXshadowXx")]
    identities = _identities(bindings={"2": "Jjew"})

    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: False, identities=identities
    )

    assert split.eligible == ["Jjew"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_items_raffle.py -k "identity or classify or binding or fallback or skipped" -v`
Expected: FAIL — `module 'items_raffle' has no attribute 'Identities'`.

- [ ] **Step 3: Add `Identities` and extend `VoterSplit`**

In `items_raffle.py`, replace the existing `VoterSplit` dataclass and add
`Identities` above it:

```python
@dataclass(frozen=True)
class Identities:
    """The three places a Discord account can be tied to a roster row.

    Passed in rather than read here so this module stays pure: the bot
    binds it from the state it already holds.
    """

    bindings: dict[str, str]
    not_players: frozenset[str]
    request_igns: dict[str, str]


@dataclass(frozen=True)
class VoterSplit:
    eligible: list[str] = field(default_factory=list)
    already_have: list[str] = field(default_factory=list)
    # Blocks the freeze: a voter nobody can name must not be silently
    # dropped from a pool a winner is drawn from.
    unidentified: list[Voter] = field(default_factory=list)
    # Resolved only through the IGN they last requested under, which may
    # be an alt -- shown separately so an officer can check before drawing.
    from_request: list[tuple[Voter, str]] = field(default_factory=list)
    skipped: list[Voter] = field(default_factory=list)
```

- [ ] **Step 4: Add `resolve_identity`**

Add this above `classify_voters` in `items_raffle.py`:

```python
def _in_roster(ign: str, roster: list[str]) -> str | None:
    """The roster spelling of this IGN, or None when it is not one."""
    try:
        return items_rules.resolve_ign(ign, roster)
    except items_rules.RequestParseError:
        # Two roster rows normalise identically. That is a sheet problem,
        # and nobody can be resolved safely against it.
        return None


def resolve_identity(
    voter: Voter, roster: list[str], identities: Identities
) -> tuple[str | None, str | None]:
    """The roster row this voter is, and which rung of the ladder said so.

    A binding is tried before the nickname so an officer can correct a
    nickname that resolves to the wrong player. A binding or fallback
    naming a row that has since left the sheet resolves to nothing rather
    than staying drawable.
    """
    key = str(voter.user_id)

    bound = identities.bindings.get(key)
    if bound is not None:
        player = _in_roster(bound, roster)
        return (player, "binding") if player is not None else (None, None)

    if key in identities.not_players:
        return None, "skipped"

    player = resolve_voter(voter.display_name, roster)
    if player is not None:
        return player, "nickname"

    requested = identities.request_igns.get(key)
    if requested is not None:
        player = _in_roster(requested, roster)
        return (player, "request") if player is not None else (None, None)

    return None, None
```

- [ ] **Step 5: Rewrite `classify_voters`**

Replace the body of `classify_voters` in `items_raffle.py`, keeping its
docstring's first paragraph and adding the ladder note:

```python
def classify_voters(
    voters: list[Voter],
    roster: list[str],
    holds: Callable[[str], bool],
    identities: Identities,
) -> VoterSplit:
    """Split the poll's voters into the groups an officer needs.

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
    from_request: list[tuple[Voter, str]] = []
    skipped: list[Voter] = []
    seen: set[str] = set()

    for voter in voters:
        player, source = resolve_identity(voter, roster, identities)
        if source == "skipped":
            skipped.append(voter)
            continue
        if player is None:
            unidentified.append(voter)
            continue
        key = normalize(player)
        if key in seen:
            continue
        seen.add(key)
        if source == "request":
            from_request.append((voter, player))
        (already_have if holds(player) else eligible).append(player)

    return VoterSplit(
        eligible=eligible,
        already_have=already_have,
        unidentified=unidentified,
        from_request=from_request,
        skipped=skipped,
    )
```

- [ ] **Step 6: Update the one call site so the suite stays green**

In `items_bot.py`, `list_cmd` calls `classify_voters` with three arguments.
Change that call to pass the real identity sources now — Task 4 depends on it
and there is no reason to pass an empty placeholder:

```python
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
```

- [ ] **Step 7: Run the tests**

Run: `./.venv/bin/python -m pytest tests/test_items_raffle.py -v`
Expected: PASS.

- [ ] **Step 8: Run the full suite**

Run: `./.venv/bin/python -m pytest -q`

Existing `!list` tests may now resolve MORE voters than before, because the
`!request` fallback is live. That is the intended behaviour. If a test fails
because a voter it expected in "Couldn't identify" is now resolved, update the
test's expectation — but do NOT weaken an assertion that checks who ended up
in `eligible`.

Expected after any such updates: 0 failures.

- [ ] **Step 9: Commit**

```bash
git add items_raffle.py items_bot.py tests/test_items_raffle.py tests/test_items_bot.py
git commit -m "Resolve a voter through bindings, nickname, then last request"
```

---

### Task 3: The `!iam`, `!bind` and `!notaplayer` commands

**Files:**
- Modify: `items_bot.py` — add `raffle_member_access`, the three commands, and
  extend `_RAFFLE_COMMANDS`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `items_state.State.bindings` / `.not_players` (Task 1);
  `items_rules.resolve_ign`; existing `raffle_access`, `_refuse_raffle`,
  `IGNORE`, `error_embed`, `ok_embed`, `save_state`, `items_state.fits`.
- Produces: `items_bot.raffle_member_access(ctx) -> str | None`; commands
  `iam`, `bind`, `notaplayer`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_bot.py`. Helpers `_configured_raffle` (line 1933),
`_raffle_ctx` (1805), `_sheet` (1906) and `FakeMember` (1795) already exist.
`_raffle_ctx(roles=())` produces an author with no raffle role.

```python
def test_iam_binds_a_member_to_their_roster_row(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[], display_name="xXshadowXx")

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Kobe"))

    assert items_bot._STATE.bindings["7"] == "Kobe"
    assert ctx.sent[-1]["embed"].title.startswith("✅")


def test_iam_needs_no_raffle_role(monkeypatch):
    """Ordinary members must be able to identify themselves."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    assert items_bot._STATE.bindings["7"] == "Jjew"


def test_iam_is_silent_outside_the_raffle_channel(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(channel_id=999, roles=())

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    assert ctx.sent == []
    assert items_bot._STATE.bindings == {}


def test_iam_refuses_an_ign_another_account_already_claimed(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    items_bot._STATE.bindings["5"] = "Kobe"
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Kobe"))

    assert "already" in ctx.sent[-1]["embed"].description.casefold()
    assert items_bot._STATE.bindings == {"5": "Kobe"}


def test_iam_lets_a_member_rebind_themselves(monkeypatch):
    """Changing main must not need an officer."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    items_bot._STATE.bindings["7"] = "Jjew"
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Kobe"))

    assert items_bot._STATE.bindings["7"] == "Kobe"


def test_iam_refuses_a_name_not_in_the_roster(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Nobody"))

    assert "No player named" in ctx.sent[-1]["embed"].description
    assert items_bot._STATE.bindings == {}


def test_iam_refuses_a_member_an_officer_marked_not_a_player(monkeypatch):
    """Letting them clear it themselves would make the mark meaningless."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    items_bot._STATE.not_players.append("7")
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    assert "officer" in ctx.sent[-1]["embed"].description.casefold()
    assert items_bot._STATE.bindings == {}


def test_bind_overrides_another_accounts_claim(monkeypatch):
    """One IGN maps to at most one account."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    items_bot._STATE.bindings["5"] = "Kobe"
    ctx, _ = _raffle_ctx()

    asyncio.run(
        items_bot.bind_cmd.callback(ctx, FakeMember(user_id=7), argument="Kobe")
    )

    assert items_bot._STATE.bindings == {"7": "Kobe"}
    assert "5" in ctx.sent[-1]["embed"].description


def test_bind_clears_a_not_a_player_mark(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    items_bot._STATE.not_players.append("7")
    ctx, _ = _raffle_ctx()

    asyncio.run(
        items_bot.bind_cmd.callback(ctx, FakeMember(user_id=7), argument="Jjew")
    )

    assert items_bot._STATE.bindings["7"] == "Jjew"
    assert items_bot._STATE.not_players == []


def test_bind_needs_a_raffle_role(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, _ = _raffle_ctx(roles=())

    asyncio.run(
        items_bot.bind_cmd.callback(ctx, FakeMember(user_id=7), argument="Jjew")
    )

    assert items_bot._STATE.bindings == {}


def test_notaplayer_marks_and_clears_any_binding(monkeypatch):
    _configured_raffle(monkeypatch)
    ctx, _ = _raffle_ctx()
    items_bot._STATE.bindings["7"] = "Jjew"

    asyncio.run(items_bot.notaplayer_cmd.callback(ctx, FakeMember(user_id=7)))

    assert items_bot._STATE.not_players == ["7"]
    assert items_bot._STATE.bindings == {}


def test_a_binding_that_will_not_fit_is_rolled_back(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    monkeypatch.setattr(items_state, "fits", lambda state: False)
    ctx, _ = _raffle_ctx(roles=())
    ctx.author = FakeMember(user_id=7, roles=[])

    asyncio.run(items_bot.iam_cmd.callback(ctx, argument="Jjew"))

    assert items_bot._STATE.bindings == {}
    assert "full" in ctx.sent[-1]["embed"].description.casefold()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_items_bot.py -k "iam or bind or notaplayer" -v`
Expected: FAIL — `module 'items_bot' has no attribute 'iam_cmd'`.

- [ ] **Step 3: Add `raffle_member_access`**

In `items_bot.py`, immediately after `raffle_access`, add:

```python
def raffle_member_access(ctx) -> str | None:
    """None if a MEMBER command may run here, else a refusal or IGNORE.

    Same channel confinement as raffle_access and the same silence when
    typed elsewhere, without the role check: !iam exists so that members
    who hold no raffle role can identify themselves.
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
    return None
```

- [ ] **Step 4: Extend the channel guard**

Change `_RAFFLE_COMMANDS` in `items_bot.py`:

```python
_RAFFLE_COMMANDS = frozenset({
    "poll", "list", "winner", "cancelpoll", "iam", "bind", "notaplayer",
})
```

- [ ] **Step 5: Add a shared save-or-rollback helper**

All three commands change state the same way. Add this above the commands:

```python
async def _save_binding_change(ctx, undo: Callable[[], None]) -> bool:
    """Persist a binding change, or undo it and say so. True when saved."""
    if not items_state.fits(_STATE):
        undo()
        await ctx.send(
            embed=error_embed(
                "State is full",
                "The bot cannot store another binding until the request "
                "queue is worked down. Nothing was changed.",
            )
        )
        return False
    channel = (
        bot.get_channel(_STATE.officer_channel_id)
        if _STATE.officer_channel_id is not None
        else None
    )
    if channel is not None:
        await save_state(channel)
    return True
```

`Callable` is NOT currently imported in `items_bot.py`. Add
`from collections.abc import Callable` beside the other stdlib imports at the
top of the file (after `import sys`, before `from difflib import ...`).

- [ ] **Step 6: Add the three commands**

Add these near the other raffle commands in `items_bot.py`, and remember the
file's last statement must remain the `if __name__ == "__main__":` block:

```python
@bot.command(name="iam")
async def iam_cmd(ctx, *, argument: str = ""):
    """Bind your own Discord account to your roster row."""
    if await _refuse_raffle(ctx, raffle_member_access(ctx)):
        return

    caller = str(ctx.author.id)
    if caller in _STATE.not_players:
        await ctx.send(
            embed=error_embed(
                "Not allowed",
                "An officer has marked this account as not a roster player. "
                "Ask an officer to run `!bind` for you.",
            )
        )
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        try:
            player = items_rules.resolve_ign(argument.strip(), snapshot.roster)
        except items_rules.RequestParseError as exc:
            await ctx.send(embed=error_embed("Not recorded", str(exc)))
            return
        if player is None:
            suggestions = get_close_matches(argument.strip(), snapshot.roster, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            await ctx.send(
                embed=error_embed(
                    "Not recorded",
                    f"No player named {argument.strip()!r} in the sheet.{hint} "
                    "Usage: `!iam <your IGN>`",
                )
            )
            return

        holder = next(
            (
                user_id
                for user_id, ign in _STATE.bindings.items()
                if user_id != caller
                and items_rules.normalize(ign) == items_rules.normalize(player)
            ),
            None,
        )
        if holder is not None:
            await ctx.send(
                embed=error_embed(
                    "Not recorded",
                    f"**{player}** is already claimed by <@{holder}>. If that "
                    "is wrong, ask an officer to run `!bind`.",
                )
            )
            return

        previous = _STATE.bindings.get(caller)
        _STATE.bindings[caller] = player

        def undo():
            if previous is None:
                _STATE.bindings.pop(caller, None)
            else:
                _STATE.bindings[caller] = previous

        if not await _save_binding_change(ctx, undo):
            return

    await ctx.send(
        embed=ok_embed(
            "You are recorded",
            f"This account is **{player}**. You will be recognised in raffle "
            "polls from now on.",
        )
    )


@bot.command(name="bind")
async def bind_cmd(ctx, member: discord.Member, *, argument: str = ""):
    """Bind someone else's Discord account to a roster row."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        try:
            player = items_rules.resolve_ign(argument.strip(), snapshot.roster)
        except items_rules.RequestParseError as exc:
            await ctx.send(embed=error_embed("Not recorded", str(exc)))
            return
        if player is None:
            suggestions = get_close_matches(argument.strip(), snapshot.roster, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            await ctx.send(
                embed=error_embed(
                    "Not recorded",
                    f"No player named {argument.strip()!r} in the sheet.{hint} "
                    "Usage: `!bind @user <IGN>`",
                )
            )
            return

        target = str(member.id)
        # One IGN maps to at most one account, or two voters would resolve
        # to the same row and one of them would be silently collapsed away.
        displaced = [
            user_id
            for user_id, ign in _STATE.bindings.items()
            if user_id != target
            and items_rules.normalize(ign) == items_rules.normalize(player)
        ]
        previous = dict(_STATE.bindings)
        was_marked = target in _STATE.not_players
        for user_id in displaced:
            _STATE.bindings.pop(user_id, None)
        _STATE.bindings[target] = player
        if was_marked:
            _STATE.not_players.remove(target)

        def undo():
            _STATE.bindings.clear()
            _STATE.bindings.update(previous)
            if was_marked and target not in _STATE.not_players:
                _STATE.not_players.append(target)

        if not await _save_binding_change(ctx, undo):
            return

    taken = (
        " Removed the earlier claim by "
        + ", ".join(f"<@{user_id}>" for user_id in displaced)
        + "."
        if displaced
        else ""
    )
    await ctx.send(
        embed=ok_embed(
            "Binding recorded",
            f"<@{member.id}> is **{player}**.{taken}",
        )
    )


@bot.command(name="notaplayer")
async def notaplayer_cmd(ctx, member: discord.Member):
    """Record that this account has no roster row at all."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    # No IGN to resolve, so no sheet read -- but the lock is still held
    # while _STATE is mutated and saved, so a concurrent !list cannot
    # classify voters against a half-applied change.
    async with _SHEET_LOCK:
        target = str(member.id)
        previous = _STATE.bindings.get(target)
        already_marked = target in _STATE.not_players
        _STATE.bindings.pop(target, None)
        if not already_marked:
            _STATE.not_players.append(target)

        def undo():
            if previous is not None:
                _STATE.bindings[target] = previous
            if not already_marked and target in _STATE.not_players:
                _STATE.not_players.remove(target)

        if not await _save_binding_change(ctx, undo):
            return

    await ctx.send(
        embed=ok_embed(
            "Marked as not a player",
            f"<@{member.id}> has no roster row and will be skipped in raffle "
            "polls. Run `!bind` to undo this.",
        )
    )
```

- [ ] **Step 7: Run the tests**

Run: `./.venv/bin/python -m pytest tests/test_items_bot.py -k "iam or bind or notaplayer" -v`
Expected: PASS.

- [ ] **Step 8: Run the full suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: 0 failures.

- [ ] **Step 9: Extend the two existing command-coverage tests**

Neither is exhaustive, so neither fails — but both exist to catch exactly the
mistake of adding a command and forgetting to wire it up.

In `tests/test_items_bot.py:2664`, add the three names to the tuple in
`test_every_raffle_command_is_registered_on_the_bot`:

```python
    for name in ("poll", "list", "winner", "cancelpoll", "iam", "bind",
                 "notaplayer", "setraffleroles", "setrafflechannel"):
```

In `tests/test_items_bot.py:2919`, add them to the raffle-channel tuple in
`test_officer_and_raffle_commands_use_their_own_channels`:

```python
    for name in ("poll", "list", "winner", "cancelpoll", "iam", "bind",
                 "notaplayer"):
```

Run: `./.venv/bin/python -m pytest tests/test_items_bot.py -k "registered or own_channels" -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Add !iam, !bind and !notaplayer"
```

---

### Task 4: `!list` refuses to freeze while anyone is unresolved

**Files:**
- Modify: `items_bot.py` — `render_pool`, `list_cmd`
- Test: `tests/test_items_bot.py`

**Interfaces:**
- Consumes: `VoterSplit.unidentified` / `.from_request` / `.skipped` (Task 2).
- Produces: no new public names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_items_bot.py`. `_open_raffle` (line 2083) and
`FakePoll` already exist. These tests replace `items_bot.poll_voters`
wholesale with the `_fake_poll_voters` helper defined at the end of this
step, so no Discord poll object is involved.

```python
def test_list_refuses_to_freeze_while_a_voter_is_unresolved(monkeypatch):
    """A voter nobody can name must not be silently dropped from the pool."""
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "xXshadowXx")]),
    )

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is False, "the pool must NOT be frozen"
    assert raffle.eligible == ()
    description = ctx.sent[-1]["embed"].description
    assert "<@2>" in description
    assert "xXshadowXx" in description
    assert "!iam" in description


def test_list_freezes_once_the_unresolved_voter_is_bound(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew", "Kobe"))
    items_bot._STATE.bindings["2"] = "Kobe"
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "xXshadowXx")]),
    )

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is True
    assert raffle.eligible == ("Jjew", "Kobe")


def test_list_freezes_when_the_only_stranger_is_marked_not_a_player(monkeypatch):
    _configured_raffle(monkeypatch)
    _sheet(monkeypatch, roster=("Jjew",))
    items_bot._STATE.not_players.append("2")
    ctx, channel = _raffle_ctx()
    _open_raffle(channel, ends="2026-08-09 10:00:00")
    monkeypatch.setattr(
        items_bot, "poll_voters",
        _fake_poll_voters([(1, "BK | Jjew"), (2, "a guest")]),
    )

    asyncio.run(items_bot.list_cmd.callback(ctx, argument="Asta's Heart"))

    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle.listed is True
    assert raffle.eligible == ("Jjew",)
    assert "1 voter skipped" in ctx.sent[-1]["embed"].description


def test_render_pool_shows_the_request_fallback_group():
    voter = items_raffle.Voter(user_id=3, display_name="xXshadowXx")
    split = items_raffle.VoterSplit(
        eligible=["Jjew", "Ryuu"], from_request=[(voter, "Ryuu")]
    )

    text = items_bot.render_pool("Asta's Heart", split)

    assert "last !request" in text
    assert "<@3>" in text
    assert "Ryuu" in text
```

`_fake_poll_voters` is a new helper for these tests — add it beside them:

```python
def _fake_poll_voters(pairs):
    """Stand in for poll_voters, which would otherwise hit Discord."""
    async def _voters(message):
        return [
            items_raffle.Voter(user_id=user_id, display_name=name)
            for user_id, name in pairs
        ]
    return _voters
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_items_bot.py -k "unresolved or freezes or fallback_group" -v`
Expected: FAIL — the pool freezes today regardless of unidentified voters, so
`raffle.listed` is True where the first test expects False.

- [ ] **Step 3: Refuse the freeze in `list_cmd`**

In `items_bot.py`, in `list_cmd`, immediately AFTER the `split = items_raffle.classify_voters(...)`
call and BEFORE `updated = items_state.replace_raffle(...)`, insert:

```python
        if split.unidentified:
            # Freezing now would drop these voters from the pool a winner
            # is drawn from, and nothing later would reveal that it happened.
            lines = [
                f"<@{voter.user_id}>  nickname {voter.display_name!r}"
                for voter in split.unidentified
            ]
            await ctx.send(
                embed=error_embed(
                    "Pool not frozen",
                    f"{len(split.unidentified)} voter(s) could not be "
                    f"identified:\n\n" + "\n".join(lines) + "\n\nThey must run "
                    "`!iam <IGN>`, or an officer runs `!bind @user <IGN>` or "
                    "`!notaplayer @user`. Then run `!list` again.",
                )
            )
            return
```

- [ ] **Step 4: Render the two new groups**

In `render_pool`, after the existing `if split.unidentified:` block, replace
that block and add the new ones. The unidentified group no longer appears
here — `list_cmd` returns before rendering when it is non-empty — so delete
it and add:

```python
    if split.from_request:
        block = "ℹ️ **Identified from their last !request** — check these"
        entries = [
            f"<@{voter.user_id}> → {ign}  (nickname {voter.display_name!r})"
            for voter, ign in split.from_request
        ]
        lines += ["", block, _capped(entries, max(budget, 0), "\n")]
        budget -= len(lines[-1]) + len(block)
    if split.skipped:
        count = len(split.skipped)
        noun = "voter" if count == 1 else "voters"
        lines += ["", f"_{count} {noun} skipped (not roster players)_"]
```

- [ ] **Step 5: Run the tests**

Run: `./.venv/bin/python -m pytest tests/test_items_bot.py -k "unresolved or freezes or fallback_group" -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `./.venv/bin/python -m pytest -q`

Existing tests that expected `!list` to freeze a pool containing an
unidentifiable voter will now fail. That is the point of this task. For each,
bind the voter or mark them not-a-player in the fixture so the freeze
proceeds — do NOT weaken an assertion about who ends up in `eligible`, and do
NOT delete a test to make it pass.

Expected after those updates: 0 failures.

- [ ] **Step 7: Commit**

```bash
git add items_bot.py tests/test_items_bot.py
git commit -m "Refuse to freeze a raffle pool while a voter is unresolved"
```

---

### Task 5: Document the binding commands

**Files:**
- Modify: `items_bot.py` — `itemhelp_cmd`
- Modify: `README.md` — the raffle command table and the paragraph after it
- Modify: `docs/item-bot-setup.md` — the raffle command block
- Test: `tests/test_items_bot.py` — the existing help test

**Interfaces:**
- Consumes: the command names from Task 3.
- Produces: nothing.

- [ ] **Step 1: Update the help embed**

In `itemhelp_cmd`, after the `!cancelpoll` line, add:

```python
            "\n**`!iam <your IGN>`** — tell the bot which player you are\n"
            "**`!bind @user <IGN>`** — officer: identify someone\n"
            "**`!notaplayer @user`** — officer: they have no roster row"
```

- [ ] **Step 2: Update `README.md`**

Add these rows to the raffle command table, after the `!winner` rows:

```markdown
| `!iam <your IGN>` | Tell the bot which roster row your Discord account is. Needed once, by any member whose nickname the bot cannot match. |
| `!bind @user <IGN>` | Officer: identify someone else, overriding their nickname and any earlier claim on that IGN. |
| `!notaplayer @user` | Officer: record that this account has no roster row, so it stops blocking raffles. Undone by `!bind`. |
```

Then replace the paragraph beginning "**Nicknames must contain the IGN.**"
with:

```markdown
**Every voter must be identifiable.** The bot matches each voter's server
nickname against the sheet, stripping the guild tag, so `BK | Jjew`,
`M2 - Jjew`, `BK Jjew` and a bare `Jjew` all reach the same row. When a
nickname does not match, it falls back to the IGN that account last used with
`!request`, and shows you which voters it resolved that way.

If it still cannot name someone, `!list` **refuses to freeze the pool** and
names them. Nobody who voted is ever silently left out of a draw. Fix it with
`!iam`, `!bind` or `!notaplayer`, then run `!list` again.
```

- [ ] **Step 3: Update `docs/item-bot-setup.md`**

Add these lines to the raffle command block, keeping every description
starting at the same column as the existing lines:

```
!iam <your IGN>                          any member: which player you are
!bind @user <IGN>                        officer: identify someone else
!notaplayer @user                        officer: they have no roster row
```

Then add this paragraph after the one describing `!list`:

```markdown
`!list` refuses to freeze the pool while any voter is unidentified, so a
member the bot cannot name is never dropped from a draw without anyone
noticing. Members fix this themselves with `!iam <IGN>`; an officer can use
`!bind @user <IGN>`, or `!notaplayer @user` for a guest who has no row in the
sheet at all.
```

- [ ] **Step 4: Run the help test**

Run: `./.venv/bin/python -m pytest tests/test_items_bot.py -k help -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `./.venv/bin/python -m pytest -q`
Expected: 0 failures.

- [ ] **Step 6: Commit**

```bash
git add items_bot.py README.md docs/item-bot-setup.md
git commit -m "Document the voter binding commands"
```
