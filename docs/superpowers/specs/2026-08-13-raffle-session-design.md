# A raffle session draws every poll in turn, and nobody wins twice

Date: 2026-08-13

## The problem

A guild opens several special log polls at once and draws them all in one
sitting. The rule for that sitting is that a player may win only once:
whoever wins the first log is out of the running for every log drawn
after it.

The bot cannot enforce that today. Each raffle is drawn independently —
`!list` freezes one poll's pool and `!winner` records one poll's draw —
and neither command knows another draw happened five minutes ago. The
officer has to hold the list of who has already won in their head and
mentally strike those names off every later pool. Nothing catches the
mistake when they forget.

## The idea

Replace the per-poll commands with one guided sitting: a **raffle
session**.

The officer runs `!startraffle`. The bot takes every poll that has closed
and has no winners recorded, oldest first, and walks through them one at
a time. For each poll it posts the eligible list and stops. The officer
draws by hand, off the bot, exactly as they do today, then types the
winners. The bot records them, and only then posts the next poll's list.

Each winner joins a list held by the session, and every pool posted after
that has those names removed. When the last poll is done the bot posts a
summary of the whole sitting and the session ends.

The draw itself stays manual. The bot never picks a name; it decides who
is allowed to be picked, and records who was.

## What is removed

`!winner` and `!list` are deleted. A winner can be recorded **only**
through a session, and the session posts the pools itself, so neither
command has a job left. Keeping them would leave a way to draw a poll out
of order and hand a log to a player the session was about to exclude —
which is the exact mistake this feature exists to prevent.

The raffle becomes five commands:

```
!poll <special log> [--hours N]   open a poll
!cancelpoll <special log>         cancel an open poll
!startraffle                      begin the sitting
!won <IGN>                        record the current poll's winner
!won <IGN> - <IGN>                several winners for one poll
!skipraffle                       leave the current poll undrawn, move on
```

`!iam`, `!bind` and `!notaplayer` are unchanged except for the auto-retry
described below.

## The session

### What goes into it

Every raffle in state whose poll has closed (`ends_at` is in the past)
and which is not `drawn`, ordered by `created_at` ascending — the order
the polls were opened. A raffle that was skipped in an earlier session is
undrawn and so is picked up again by the next one.

`!startraffle` with no such raffles refuses and says so. `!startraffle`
while a session is already active retries the current poll (see
"Auto-retry" below) rather than starting a second session.

### What the session stores

The session lives in the bot's pinned state, not in memory. The bot runs
on Render's free tier and restarts; a session held in memory would
evaporate mid-sitting and take the "already won" list with it, silently
making an excluded player eligible again on the next pool.

It stores:

- the ordered log names in the sitting
- which position is current
- the IGNs that have won so far in this session
- the per-log outcome so far (drawn / skipped) for the closing summary

It does **not** store the pools. Those already live on each `Raffle` as
`eligible` / `listed` and stay there.

### Freezing a pool

When a poll's turn arrives the bot freezes its pool exactly the way
`!list` does today: fetch the poll message, confirm Discord has closed
voting, read the sheet, classify the voters, drop anyone whose checkbox
for that log is already ticked, and store the result as `eligible` with
`listed=True`.

A poll already frozen by an earlier session replays its stored pool
verbatim rather than recomputing it. The pool a winner is drawn from must
not be able to change between the officer looking at it and drawing from
it — and after a restart, the list that comes back must be the same list
that was on screen.

### Excluding this session's winners

The session's winners are subtracted from the pool at the moment it is
**displayed** and at the moment a `!won` name is **validated**. The
subtraction is never written into the stored `eligible`.

That keeps `eligible` meaning what it has always meant — who this poll
made eligible — so a raffle read back later, by a future feature or by a
human reading the pin, is not silently missing people for a reason
nothing in the record explains.

Names are compared through `items_rules.normalize`, the same comparison
every other name check in the raffle uses, so an alias cannot slip a
session winner back into a later pool.

Excluded players are shown in their own group under the pool —
"Won earlier this session" — rather than just vanishing. A pool that
shrinks with no visible reason is how a wrong exclusion goes unnoticed.

### Recording a winner

`!won <IGN>` or `!won <IGN> - <IGN> - <IGN>`. The session already knows
which log is current, so the argument is only names.

Every name is resolved against the roster and checked against the current
filtered pool **before the first sheet write**, so a typo in the third
name cannot leave the first two ticked. The writes themselves go through
the existing `items_sheet.commit_approval` path with its existing
handling: a ledger row that fails to write is handed back as a pasteable
row with a "do not re-run for this player" warning, an already-ticked
checkbox is reported rather than written twice, and a hard failure part
way through records what succeeded, leaves the poll current, and tells
the officer to re-run `!won` with only the remaining names.

On success the winners join the session's winner list, the raffle is
marked `drawn`, the position advances, and the next poll's list posts.

`!won` with no active session refuses: "No raffle session is running. Run
`!startraffle` first."

### Skipping

`!skipraffle` advances the position and leaves the current raffle
undrawn, so a later session picks it up again. It is also the answer when
a pool comes out empty — the bot says the pool is empty but still waits,
because an empty pool is sometimes a sign that something is wrong with
the identities rather than a poll nobody entered.

There is deliberately no abort command. A session ends by reaching the
end of its polls, whether each one was drawn or skipped.

### Unidentified voters

Freezing refuses while any voter cannot be matched to a roster row, the
same refusal `!list` makes today. Freezing anyway would drop that voter
from a pool a winner is drawn from, and nothing later would reveal it
happened.

The session holds on that poll and names the voters. The rest of the
sitting is untouched.

### Auto-retry

Once the identity is fixed, the stuck poll is retried automatically.
`!iam`, `!bind` and `!notaplayer` each check, after saving, whether a
session is currently held on an unidentified voter; if so they re-freeze
and re-post that poll.

All three are already confined to the raffle channel, so the retry posts
where the session is running. `!iam` is included alongside the two
officer commands because it fixes the same condition by the same
mechanism — the member naming themselves is often the fastest way out of
the block.

A retry that still finds an unidentified voter simply refuses again. The
retry never advances the session and never writes to the sheet, so a
failed one costs nothing.

`!startraffle` during an active session performs the same retry, so there
is a way to re-attempt by hand if the auto-retry is missed.

### Ending

After the last poll the bot posts one summary — every log in the sitting
with its winners, or marked as skipped — and clears the session from
state.

## Shape of the code

The split the existing bot already uses is kept: everything decidable
without Discord or Google Sheets lives in a pure module and is unit
tested; the command functions do I/O and orchestration.

### `items_raffle.py`

Deleted: `split_item_and_ign`, `split_item_and_igns`, `WINNER_USAGE`.
About 120 lines existed to answer "the log name and the IGN both contain
spaces, so where does one end and the other begin?" — a question `!won`
does not have, because the session supplies the log.

Added: `split_igns(argument, roster) -> list[str]`, which keeps the parts
of that logic that still matter — splitting on a hyphen only when it has
whitespace on both sides, so `wile-KAMOTE` survives intact; resolving
each name against the roster with a "did you mean" suggestion on a miss;
and refusing a name listed twice, since a player cannot win one log
twice.

Added: the session's pure decisions — selecting and ordering the raffles
for a sitting, filtering a frozen pool against the session's winners,
advancing the position, and building the summary.

### `items_state.py`

Added: a `RaffleSession` dataclass with `to_dict` / `from_dict`, stored
on `State` and defaulting to `None`. A pin written before this feature
has no session key and must load as "no session" rather than failing, the
same way the multi-winner change handled an older pin's single `winner`
string.

The session must be counted by `items_state.fits`, so a sitting that
cannot be persisted is refused at `!startraffle` rather than quietly
breaking every later save.

### `items_bot.py`

Deleted: `winner_cmd`, `list_cmd`.

Two helpers are extracted from those two commands before they go, because
both halves are needed by the session and neither should exist twice:

- the pool freeze — fetch poll, confirm closed, read sheet, classify,
  refuse on unidentified, store, save
- the winner write — the `commit_approval` loop with its ledger-gap,
  already-ticked and partial-failure reporting

Added: `startraffle_cmd`, `won_cmd`, `skipraffle_cmd`, and the
advance-and-post step that runs after a successful `!won` or
`!skipraffle`.

`_RAFFLE_COMMANDS` loses `list` and `winner` and gains `startraffle`,
`won` and `skipraffle`, so the new commands are confined to the raffle
channel like the rest. `test_every_registered_command_is_classified`
already guards that this is not forgotten.

`render_pool` stays and gains the "Won earlier this session" group. Its
existing character budgeting is kept: the eligible list is what must
survive truncation intact.

### Docs

`README.md` and `docs/item-bot-setup.md` have their raffle sections
rewritten around the session. Both currently document `!list` and
`!winner` as the way to draw, which would be actively wrong.

## Testing

- `tests/test_items_raffle.py` — `split_igns` replaces the
  `split_item_and_*` tests; new tests for session selection and ordering,
  pool filtering against session winners including an alias, and the
  summary.
- `tests/test_items_state.py` — a session round-trips through a pin; a
  pin with no session loads as no session; a session is counted by
  `fits`.
- `tests/test_items_bot.py` — a full session walkthrough across several
  polls proving a winner disappears from the later pools; `!won` refused
  with no session; `!won` with a name not in the filtered pool writes
  nothing; the session holds on an unidentified voter and `!bind`
  auto-retries it; `!skipraffle` leaves the raffle undrawn and a later
  session picks it up; a partial sheet failure leaves the poll current;
  the session survives a state reload mid-sitting.

## Accepted behaviour worth naming

A skipped poll keeps its frozen pool. Picked up in a later session it
replays that stored pool rather than re-reading the sheet, so a player
who received that log by officer distribution in the meantime would still
appear eligible. This is already true of the current freeze-then-draw
design and is not changed here.

## Not in scope

The bot still does not draw. No randomness is added anywhere; the officer
draws by hand and types the result. Nothing about `!request`, the gear
cap, the queue, the `!distribute` panel or the public board changes.
