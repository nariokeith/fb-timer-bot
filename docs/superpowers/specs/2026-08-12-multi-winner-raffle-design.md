# Multi-winner special log raffle

Date: 2026-08-12

## Problem

A special log drop can be multiple copies of the same item — three Amentis
Foot from one week's runs. Today one poll can produce exactly one winner:
`Raffle.winner` is a single string, and `!winner` refuses any raffle that
already has one (`items_bot.py:1759`).

The workaround is to run `!poll` once per copy. That costs three polls and
three voting periods for one draw, and members must vote three times for
the same item. Officers want one poll, one draw, several winners.

## Command syntax

```
!winner Amentis Foot A - B - C        item runs into the first name
!winner Amentis Foot - A - B - C      dash after the item
!winner Amentis Foot A                single winner, unchanged
```

Winners are separated by a hyphen with whitespace on **both** sides,
matched as `\s+-\s+` so repeated spaces are tolerated.

The spaces are load-bearing. `items_raffle.SEPARATORS` already treats `-`
as a guild-tag separator, and the roster contains hyphenated rows
(`wile-KAMOTE`) and multi-word rows (`chinchong ni Mumu`). A bare `-`
would shred the first; requiring surrounding whitespace leaves both
intact.

Both item positions are accepted because officers type either naturally
and neither is ambiguous.

## Parsing

New `items_raffle.split_item_and_igns(argument, item_names, roster) ->
tuple[str, list[str]]`.

1. Split `argument` on `\s+-\s+` into chunks.
2. If chunk 1 normalizes to exactly one of `item_names`, that is the item
   and every later chunk is an IGN.
3. Otherwise run the existing `split_item_and_ign` on chunk 1 to get
   (item, first IGN); later chunks are additional IGNs.
4. Resolve each later chunk with `items_rules.resolve_ign`.

The existing `split_item_and_ign` is kept unchanged. It is the tested core
of case 3, and its multi-reading refusal still applies to chunk 1.

Refused, before any sheet write:

- a chunk that resolves to no roster row (reported with close matches)
- two chunks resolving to the same roster row
- an empty chunk, i.e. a trailing or doubled dash
- no IGN at all after the item

`RequestParseError` from `resolve_ign` (two roster rows normalizing
identically) surfaces as `RaffleArgumentError`, matching current
behaviour.

## State schema

`Raffle.winner: str` is replaced by two fields:

| Field | Meaning |
|---|---|
| `winners: tuple[str, ...]` | players actually recorded in the sheet |
| `drawn: bool` | a `!winner` command completed with no hard failure |

Two fields rather than one, for the same reason `listed` exists: after a
partial failure `winners` is non-empty while the raffle is unfinished, and
that state must stay distinguishable from a completed draw or the retry
path below is impossible.

`to_dict` writes `winners` and `drawn`. `from_dict` reads them when
present and otherwise migrates the legacy `{"winner": "Jjew"}` shape to
`winners=("Jjew",), drawn=True`. State lives in a pinned Discord message
that already holds raffles written under the old schema, so the fallback
is required, not optional.

Knock-on changes:

- `raffle_to_evict` / `evict_for_new_raffle` (`items_state.py:490`) evict
  only raffles where `drawn` is true. A partially drawn raffle still owns
  ticked checkboxes and an unfinished draw; dropping it would strand both.
- `!poll` (`items_bot.py:1378`, `:1394`) treats any raffle with a
  non-empty `winners` as already drawn for the purposes of refusing a
  duplicate poll and superseding an older one.
- `!cancelpoll` (`items_bot.py:1920`) refuses any raffle with a non-empty
  `winners` — a ticked checkbox cannot be cancelled.

## Writing and failure handling

Validation of every name completes before the first write. Writes then run
one name at a time through the existing `items_sheet.commit_approval`,
which is per-IGN and has no batch form (`items_sheet.py:381`).

| Outcome for a name | Action |
|---|---|
| success | recorded, continue |
| `AlreadyHeld` | counted as recorded, noted in the report, continue |
| `LedgerWriteError` | recorded, pasteable ledger row printed, continue |
| any other exception | stop; remaining names not attempted; `drawn` stays false |

`LedgerWriteError` continues rather than stopping because its checkbox is
already ticked. A retry would hit `AlreadyHeld`, skip the name, and the
missing ledger row would never be written — so the row is surfaced for
manual entry immediately, exactly as the single-winner path does today.

A hard failure leaves the raffle open. The report names what was recorded,
what failed and why, what was not attempted, and the exact command to
re-run:

```
Recorded: A
FAILED: B (sheet rate limit)
Not attempted: C
Re-run: !winner Amentis Foot B - C
```

Retyping a name already present in `winners` is refused, so a retry cannot
double-tick. When a command finishes with no hard failure, `drawn` becomes
true and any further `!winner` for that item is refused with
`already been drawn: A, B, C`.

State is saved once at the end of the command rather than once per outcome
branch.

## No declared winner count

`!poll` gains no `--winners N` flag. `!winner` accepts as many names as are
typed, bounded only by the frozen eligible list. A miscount ticks an extra
checkbox that an officer must undo by hand; that was accepted in exchange
for one fewer flag to remember mid-raffle.

## Display

`render_pool` takes `winners` instead of `winner` and renders
`🏆 **Winner: A**` for one and `🏆 **Winners: A, B, C**` for several. Its
embed budget already subtracts the footer length, so truncation of the
eligible list stays correct as the footer grows.

Documentation updated: help text (`items_bot.py:1259`), `README.md:281`,
`docs/item-bot-setup.md:237`.

## Testing

- `tests/test_items_raffle.py` — both item positions; hyphenated and
  multi-word IGNs surviving the split; duplicates; trailing dash; unknown
  name; single-winner argument unchanged.
- `tests/test_items_state.py` — round-trip of the new fields; legacy
  `winner` migration; eviction skipping a partially drawn raffle; the
  existing `fits()` shard test with 25 raffles carrying winner lists.
- `tests/test_items_bot.py` — all-success draw; partial failure then
  successful retry; `AlreadyHeld`; `LedgerWriteError`; refusal to re-draw a
  completed raffle; single-winner path unchanged; `render_pool` output for
  one and several winners.
