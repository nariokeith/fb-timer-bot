# Special logs move from requests to raffles

Date: 2026-08-09

## The idea

Special logs are no longer handed out on request. They are raffled.

An officer opens a poll for one special log in a dedicated channel.
Members who want it answer "Yes". When the poll closes, the bot turns the
list of voters into a list of IGNs, drops anyone whose checkbox is
already ticked in the `Special Logs` tab, and shows the officers the pool
to draw from. The officer draws by hand and tells the bot the winner,
which ticks that player's checkbox.

The checkbox is, and remains, the whole point. A ticked cell means "this
player already owns this special log", so a ticked player is filtered out
of every future raffle for that item. The tab is the single source of
truth for who owns what; the raffle is only a new way of deciding who
gets the next one.

`!request` keeps working exactly as it does today, but for gear logs
only. Nothing about the daily gear cap, the queue, the `!distribute`
panel, or the public queue board changes.

## Scope

Everything runs inside the existing item bot ("ukay-ukay sa bahay ni
talong"), the process started from `items_bot.py`. No new Discord
application, no new token, no new spreadsheet.

## Required setup change: the Server Members Intent

Discord returns poll voters as *users*. A user carries a global display
name; the per-server nickname (`BK | Jjew`) lives on the *member* object.
Resolving a voter to an IGN therefore needs the member, and fetching
members — whether from cache or over HTTP — requires the privileged
**Server Members Intent**.

That intent must be enabled for this application in the Discord
Developer Portal. Until it is, the bot can identify nobody and every
voter lands in the "couldn't identify" list. `items_bot.py` sets
`intents.members = True`; the portal toggle is a manual step for the
guild owner.

This also changes an existing claim in the module docstring: authorization
is no longer "the private officer channel itself, no role configuration
to drift". The raffle commands are role-gated, so that docstring is
updated rather than left to mislead.

## Removing special logs from `!request`

`items_rules.resolve_item` continues to read both tabs, but a query that
resolves to a `Special Logs` column now raises `ItemLookupError` with a
redirect instead of returning the item:

> `Asta's Heart` is a special log. Special logs are raffled in the raffle
> channel with `!poll`, not requested.

Keeping the special headers in the lookup is what makes that message
possible. Removing them would degrade the failure to "No item column
named 'Asta's Heart'", which tells a member nothing about where to go.

Because `resolve_item` can no longer return a special item,
`evaluate_request` can no longer produce a queued special request. The
`SPECIAL` branch of `items_rules.check_eligibility` is retained: the
raffle reuses it to answer "does this player already hold this log".

### Draining the queue on startup

Requests queued under the old rules must not survive. After `load_state`
succeeds, the bot removes every queued request whose `type` is `Special`,
saves the state, and posts one notice in the officers' channel naming
each dropped member and item.

Dropping silently was rejected: officers need to know who was waiting so
they can tell those members to watch for the raffle instead.

## Identity: nickname to IGN

Nicknames carry a guild tag. All of these forms occur:

```
Jjew            BK | Jjew            BK Jjew
M2 | Jjew       BK - Jjew
```

`items_raffle.resolve_voter(nickname, roster)` builds candidate strings
and resolves each through the existing `items_rules.resolve_ign`, which
does exact normalized matching plus the `ALIASES` table.

Candidates are: the whole nickname, plus the remainder of the string
after each separator occurrence (`|`, `-`, `:`, `/`, whitespace), with
leading separators and spaces stripped.

The remainder is taken as a *slice of the original string*, never
re-joined from split tokens. This is what keeps a multi-word roster row
like `chinchong ni Mumu` intact: `M2 - chinchong ni Mumu` yields the
candidate `chinchong ni Mumu`, whereas token re-joining would also have
to reconstruct the internal spacing and would corrupt IGNs containing a
hyphen.

A nickname resolves **only when exactly one distinct roster row matches**
across all its candidates. Several candidates resolving to the *same*
row is one match and succeeds. Two candidates resolving to two different
rows is ambiguous and fails.

A failure is not an error. The voter is reported in a "couldn't
identify" group, named by Discord mention, for an officer to handle by
hand. No fuzzy matching is used anywhere in this path, for the same
reason it is refused in `resolve_ign`: a wrong match ticks the wrong
person's checkbox permanently.

## State

`items_state.State` gains three fields:

- `raffle_role_ids: list[int]` — roles permitted to run the raffle
  commands
- `raffle_channel_id: int | None` — the one channel where they work
- `raffles: list[Raffle]`

`Raffle` is a frozen dataclass:

| field | meaning |
|---|---|
| `item` | canonical special log name, as it appears in the sheet header |
| `channel_id` | where the poll was posted |
| `message_id` | the poll message |
| `created_at` | PHT timestamp, `items_rules.TIMESTAMP_FORMAT` |
| `ends_at` | PHT timestamp when the poll closes |
| `eligible` | the frozen eligible IGNs; empty until `!list` runs |
| `listed` | whether `!list` has frozen the pool |
| `winner` | the winning IGN; empty until `!winner` succeeds |

`eligible` cannot carry `listed`'s meaning on its own: a raffle where
nobody was eligible is a real outcome and must be distinguishable from
one that has not been listed yet.

Raffles are written into the pinned state messages, spilling across
shards exactly as queued requests do. The first shard carries
`raffle_role_ids` and `raffle_channel_id` beside `officer_channel_id`.
Pins written before this change lack all three keys and decode to `[]`,
`None` and `[]`, the same way `queue_channel_id` and `note` were added.

### How many raffles are kept

`MAX_RAFFLES = 5`. Creating a sixth drops the oldest raffle whose poll
has already ended. If all five are still open, `!poll` refuses rather
than discarding a live raffle.

The ceiling exists because state lives in at most `MAX_SHARDS = 10`
pinned messages. `items_state.fits` is checked before saving, as it
already is for requests, and `!poll` refuses when the state would not
fit.

## Commands

| Command | Permitted to | Accepted in |
|---|---|---|
| `!setraffleroles @role [@role ...]` | Administrator | anywhere |
| `!setrafflechannel` | Administrator | the raffle channel |
| `!poll <special log> [--hours N]` | a raffle role | the raffle channel |
| `!list <special log>` | a raffle role | the raffle channel |
| `!winner <special log> <IGN>` | a raffle role | the raffle channel |

Roles are stored as ids. Holding *any* configured role is sufficient,
matching `attendance_bot._is_officer`. With no roles configured, the
three raffle commands refuse and name `!setraffleroles`; an empty role
set is a refusal, never an open door.

Outside the raffle channel the three commands are silently ignored, the
way `!distribute` is ignored outside the officer channel. A wrong-channel
command is far more likely to be a typo than an attack, and a silent
no-op does not leak the channel's existence.

`!setrafflechannel` requires the officer channel to be set first, because
that is where the state pins live — the same guard `!setqueuechannel`
already has.

Before a raffle channel is configured, "the raffle channel" matches
nothing, so the three commands would be ignored everywhere and an admin
trying them would get silence. To avoid that dead end, when
`raffle_channel_id` is `None` the commands reply with the setup
instruction *only* to a member holding Administrator, and stay silent for
everyone else. The hint reaches the one person who can act on it without
turning `!poll` into channel noise for members.

`!list` and `!winner` naming a special log that has no raffle at all
refuse and say so, rather than silently doing nothing: the channel and
role are already correct at that point, so the mistake is in the name and
the officer needs to see it.

### `!poll`

Resolves the name against the `Special Logs` headers only. A gear name is
refused with a pointer to `!request`; an unknown name gets the existing
suggestion list.

Refuses when an open raffle for that same log already exists. Different
logs may run side by side.

Posts a native Discord poll: question is the log name, one answer
("Yes"), `duration` 24 hours by default. A trailing `--hours N` overrides
it, bounded to 1–168. The flag goes at the end because the item name is
multi-word and unquoted; `--hours` cannot occur inside a sheet header.

The `Raffle` is saved only after Discord confirms the poll message, so a
failed post never leaves a raffle pointing at a message that does not
exist.

### `!list`

While the poll is open, refuses and reports the time remaining. Drawing
from a partial pool would silently exclude everyone who had not voted
yet.

Once the poll has ended — judged by the fetched poll's own expiry, not by
the bot's clock alone — it fetches the "Yes" voters, resolves each
nickname, reads one sheet snapshot, and posts three groups:

- **Eligible** — resolved, checkbox empty. This is the pool to draw from.
- **Already has it** — resolved, checkbox ticked. Shown, not hidden, so
  an officer can see the entry was received and deliberately excluded.
- **Couldn't identify** — unresolved voters, by mention.

The eligible IGNs are then frozen into the raffle record and the state is
saved. A later `!list` for the same raffle replays the frozen list
verbatim and does not recompute. Freezing is what makes "the winner must
be on the eligible list" a well-defined check, and it lets the raffle
finish even if the poll message is later deleted.

Before the pool is frozen, a deleted poll message is fatal: `!list`
refuses and says the raffle must be re-run.

### `!winner`

`!winner <special log> <IGN>` splits its argument by trying every split
point and accepting only the reading where the prefix names a known
raffle and the suffix names a known player — the same technique
`items_rules.parse_request` uses, and for the same reason: both halves
may contain spaces.

Refuses when: no raffle exists for that log, the poll is still open,
`!list` has not been run, a winner has already been drawn, or the IGN is
not on the frozen eligible list. The last refusal offers close matches
drawn from the eligible list itself.

On success, under `_SHEET_LOCK`, it calls
`items_sheet.commit_approval(item_type=SPECIAL)`, which ticks the
checkbox and appends the `Distribution Log` row. The officer recorded is
the member who ran `!winner`; the Discord user id recorded is theirs; the
request id is a fresh `items_state.new_request_id()` so the ledger row is
identifiable.

Reusing `commit_approval` rather than writing a new path is deliberate:
it already writes the cell before the ledger, refuses a checkbox ticked
by hand since the poll closed, and raises `LedgerWriteError` carrying the
exact row to paste. All three behaviours are needed here unchanged.

The raffle is then closed by recording the winner. A `LedgerWriteError`
closes it too, because the checkbox *is* written — re-running `!winner`
could only fail or double-write. The reply hands over the pasteable
ledger row exactly as `approve()` does.

## Failure handling

| Situation | Behaviour |
|---|---|
| Sheet unreachable | Existing error embed; nothing written |
| Poll message deleted before `!list` | `!list` refuses; re-run the raffle |
| Poll message deleted after `!list` | Unaffected; the pool is frozen |
| Checkbox ticked by hand after listing | `record_special` refuses; raffle stays open |
| Ledger append fails | Raffle closed; pasteable row returned |
| State would exceed the shard ceiling | `!poll` refuses; existing raffles untouched |
| Members intent off | Every voter is "couldn't identify" |

## Help text

`!itemhelp` states that `!request` is for gear logs only and describes
the three raffle commands with the raffle channel named. The admin setup
commands are listed alongside the existing ones.

## Testing

`items_raffle.py` is pure and is tested directly:

- every nickname form, including bare IGNs and multi-word roster rows
- nicknames whose IGN contains a hyphen
- ambiguous nicknames resolving to two rows — must not resolve
- alias resolution through `ALIASES`
- the eligible / already-has / unidentified split
- `--hours` parsing: absent, valid, out of range, malformed
- `!winner` argument splitting, including multi-word names on both sides

`items_state.py` gains round-trip tests for raffles: shard spill, a pin
written in the old format, and the `MAX_RAFFLES` eviction rule.

`items_bot.py` command behaviour is tested with the fake-Discord objects
already in `tests/test_items_bot.py`, extended with a fake poll exposing
voters and an expiry. Covered: role and channel gating, `!list` refusing
an open poll, `!list` replaying a frozen pool, `!winner` refusing each of
its five failure cases, and the `LedgerWriteError` path.

`items_rules.py` gains tests for `!request` refusing a special log with
the redirect message, and `items_bot` for the startup drop of queued
special requests.
