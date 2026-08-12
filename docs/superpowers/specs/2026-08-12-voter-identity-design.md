# Resolving every raffle voter to a roster row

Date: 2026-08-12

## Problem

`!list` turns poll voters into IGNs by stripping the guild tag from each
Discord nickname and matching the remainder against the sheet roster —
exact match or the three hand-maintained entries in
`attendance_roster.ALIASES`. Fuzzy matching is deliberately refused: a wrong
match credits a special log to the wrong player permanently.

Anyone whose nickname does not resolve lands in a "Couldn't identify"
group. They are @mentioned, but they are **not** in the frozen pool, and
the pool freezes anyway. A member with a decorated in-game name is silently
excluded from every raffle they enter until someone notices.

The bot cannot link a Discord account to a sheet row without some binding.
This spec adds the bindings, and makes an unresolved voter block the freeze
rather than be quietly dropped.

## Resolution ladder

`items_raffle` resolves each voter by trying these in order. First match
wins.

| # | Source | Result |
|---|---|---|
| 1 | Explicit binding (`!iam` / `!bind`) | that IGN |
| 2 | Not-a-player mark (`!notaplayer`) | skipped: not eligible, not blocking |
| 3 | Nickname (tag stripping, exact match, `ALIASES`) | that IGN |
| 4 | The IGN from their last `!request` | that IGN, flagged in the output |
| 5 | nothing matched | unresolved: **blocks the freeze** |

A binding is checked before a nickname so that an officer can correct a
nickname that resolves to the wrong row. Commands keep bindings and
not-a-player marks mutually exclusive, so their relative order never
decides a real case.

**A binding or fallback naming an IGN that is no longer in the roster is
treated as unresolved**, not silently honoured. A player removed from the
sheet must not stay drawable through a stale binding.

## Why the `!request` IGN is only a fallback

`_STATE.igns` already maps Discord ID to the IGN a member last requested
under. It is free and already populated, so it removes most of the need to
bind anyone by hand.

It is not identity, though: `items_bot.py:607` documents that requesting
for an alt is legitimate and deliberately not refused. A member whose last
request was for an alt would be entered under the alt. So voters resolved
this way are counted as resolved — they do not block the freeze — but are
listed in their own group in the `!list` output so an officer can spot a
wrong one before drawing. An explicit binding always overrides it.

## State

Two new fields on `State`, kept separate from `igns`:

| Field | Meaning |
|---|---|
| `bindings: dict[str, str]` | Discord ID -> IGN, set deliberately |
| `not_players: list[str]` | Discord IDs with no roster row |

Separate from `igns` on purpose. `igns` means "what they last requested
as"; a binding means "this account is this player". Merging them would let
a gear request silently change someone's raffle identity.

Both are absent from raffles already pinned in production, so `from_dict`
defaults them to empty — the same migration shape as `winners`/`drawn`.

`_encode_with_total` spills each collection across shards item by item.
`bindings` needs its own spill loop, modelled on the existing `igns` loop
and raising `ValueError("a binding is too large for a state shard")` on an
entry that cannot fit alone. `not_players` is a list of ID strings and
rides in shard 0 with the other whole-state configuration.

**Measured capacity:** one binding costs ~38 bytes. With 20 raffles of 40
eligible each plus 20 queued requests, 35 bound members use 12 of 20
shards, 100 use 14, and 300 use 18. Realistic guild sizes have wide
headroom. `!iam`, `!bind` and `!notaplayer` still call `items_state.fits`
before saving and roll back on refusal, exactly as `!request` does.

## Commands

```
!iam <IGN>            member binds themselves
!bind @user <IGN>     raffle officer; always wins
!notaplayer @user     raffle officer; records "no roster row"
```

Every IGN is resolved through the existing `items_rules.resolve_ign` —
exact match or `ALIASES`, never fuzzy — so a binding cannot name a row that
does not exist.

**`!iam`** refuses when that IGN is already bound to a *different* Discord
account, naming the holder and telling the member to ask an officer.
Re-binding yourself to a different IGN is allowed: a member who changes
main should not need an officer.

**`!bind`** always wins. It overwrites the target's existing binding,
clears any not-a-player mark on them, and removes any *other* account's
binding to that IGN, reporting when it does — one IGN maps to at most one
account.

**`!notaplayer`** records the mark and removes any binding on that account.
A later `!bind` undoes it.

**`!iam` refuses when the caller is marked not-a-player**, telling them to
ask an officer. Letting a member clear an officer's mark would make the
mark meaningless.

All three read the sheet roster to resolve the IGN, so each costs one
`read_snapshot`. They are one-time-per-member commands, so the volume is
low, but they take `_SHEET_LOCK` like every other sheet-reading command.

### Permissions and channel

`!bind` and `!notaplayer` use `raffle_access`, like `!poll` / `!list` /
`!winner`: raffle channel, raffle role.

`!iam` must work for ordinary members, who do not hold a raffle role, so it
needs a new `raffle_member_access(ctx)` — the same channel confinement and
the same silent IGNORE when typed elsewhere, without the role check. All
three names are added to `_RAFFLE_COMMANDS` so `channel_guard` confines
them.

`!iam` is the only path by which a non-officer writes to bot state. It is
bounded: it can only name a row that already exists in the sheet, it cannot
take an IGN another account already holds, and it can only ever change the
caller's own entry.

## What `!list` does

Unresolved voters present — **nothing is frozen**:

```
❌ Pool not frozen — 2 voters could not be identified

@someone   nickname "xXshadowXx"
@another   nickname "✧Kobe✧"

They must run !iam <IGN>, or an officer runs
!bind @user <IGN> / !notaplayer @user. Then run !list again.
```

All resolved — freezes as today, with one new group and a skipped count:

```
Eligible for Amentis Foot (12)
1. Jjew
...

Already has it (excluded)
Ryuu, Mumu

ℹ️ Identified from their last !request — check these
@someone → Kobe2  (nickname "xXshadowXx")

3 voters skipped (not roster players)
```

Everything else about `!list` is unchanged: it still refuses while the poll
is open, still freezes exactly once, still replays a frozen pool verbatim,
and still refuses a pool too large to store.

## Interfaces

`classify_voters` grows one parameter rather than three, so it stays pure
and testable:

```python
@dataclass(frozen=True)
class Identities:
    bindings: dict[str, str]       # discord id -> IGN
    not_players: frozenset[str]    # discord ids
    request_igns: dict[str, str]   # discord id -> IGN
```

`VoterSplit` gains two fields beside the existing three:

```python
from_request: list[tuple[Voter, str]]   # voter and the IGN it fell back to
skipped: list[Voter]                    # marked not-a-player
```

`unidentified` keeps its name but changes meaning: it now blocks the
freeze.

## Testing

Pure logic in `tests/test_items_raffle.py`: each rung of the ladder wins
over the one below it; a binding beats a nickname that would resolve
differently; a not-a-player voter is skipped rather than blocking; a
binding naming a row no longer in the roster is unresolved; the
`!request` fallback resolves and is reported separately.

Commands in `tests/test_items_bot.py`: `!iam` refuses a claimed IGN and
allows re-binding yourself; `!bind` overrides a binding, a mark, and
another account's claim; `!notaplayer` clears a binding; each refuses an
IGN not in the roster; `!iam` is refused outside the raffle channel and
allowed without a raffle role; each rolls back when `fits` says no.

`!list` in `tests/test_items_bot.py`: refuses to freeze while anyone is
unresolved and leaves `listed` False; freezes once they are bound; renders
the fallback group and the skipped count.

State in `tests/test_items_state.py`: round-trip of both fields; a pin
written before this change loads with them empty; the `bindings` spill loop
across shards; measured capacity at 100 and 300 bindings.
