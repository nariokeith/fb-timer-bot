# Item queue: sharded state and a paged distribute panel

Date: 2026-08-09

## The problem

`items_state.encode_state` renders the whole bot state — officer channel
id, the IGN memory, and every pending request — into a single Discord
message, which Discord caps at 2000 characters. When the render exceeds
`MAX_CONTENT` (1990), the encoder pops the **oldest** requests off the
queue until it fits and reports them as dropped. `items_bot.request_cmd`
then posts a "Queue overflowed" embed naming the members whose requests
were thrown away.

Measured on this data shape:

- one `PendingRequest` is ~149 characters of JSON
- the `igns` map for 50 members is ~1801 characters **by itself**

So with a full guild remembered, the IGN map alone consumes 90% of the
budget and *every* queued request is dropped on every save. The observed
"seven requests maximum" is not a design decision; it is whatever
happens to fit in the bytes left over.

Two things are wrong and both are fixed here:

1. Capacity is bounded by one message, and
2. the overflow policy sacrifices the member who has waited longest.

## Design

### Sharded state (`items_state.py`)

`encode_state(state)` returns `list[str]` — one body per message — and
never drops anything. Each shard is a self-contained JSON object so a
partial read degrades gracefully:

```
shard 0: {"part":0,"total":n,"officer_channel_id":…,"igns":{…},"queue":[…]}
shard k: {"part":k,"total":n,"queue":[…]}
```

Every shard carries the `ITEMS_STATE_V1` marker line and the same
fenced-JSON body format as today.

Decoding sorts shards by `part`, concatenates their queue slices in
order, and merges `igns`. The explicit `part` index is required because
Discord does not guarantee the order in which pins are returned.

**Backward compatibility is mandatory** — the bot is live and its
current pinned message has no `part` key. A decoded object missing
`part`/`total` is treated as `part 0 of 1`.

`MAX_SHARDS = 10` is the hard capacity bound: roughly 12 requests per
shard, so ~115 pending requests, comfortably under Discord's 50-pins-
per-channel limit and far above a 50-member guild's realistic peak.

### Refuse, never drop (`items_bot.py`)

`request_cmd` checks whether the queue *including* the candidate request
still encodes within `MAX_SHARDS` before committing it. If it does not,
the member is told the queue is full and to ask an officer to work it
down. Nothing already queued is ever lost. The `dropped` return value
and the "Queue overflowed" embed are deleted.

`save_state` reconciles the shard messages against the rendered list: it
edits the messages it already has, creates any additional ones it needs,
and deletes the surplus when the queue shrinks. The module-level
`_STATE_MESSAGE` becomes `_STATE_MESSAGES: list[discord.Message]`.

`load_state` collects every marker message from pins and history,
decodes them as shards, and restores the merged state. If a `part` is
missing from the set, it restores what it can and posts a loud warning
naming how many parts are gone. Partial recovery beats none; silent loss
is the failure mode this module exists to prevent.

### Paged distribute panel (`items_bot.py`)

`!distribute` posts **one** panel message showing page 1, replacing
today's one-message-per-25-requests behaviour. Page size stays at
`MAX_PANEL_OPTIONS = 25`, the Discord select-menu limit.

Component layout:

- row 0 — the request dropdown (25 options)
- row 1 — Approve / Deny
- row 2 — numbered page buttons, windowed around the current page, with
  ◀ ▶ arrows once there are more than five pages

A page button redraws that page in the same message. After an approve or
deny, the panel refreshes its current page; if that page has just become
empty, it falls back to the last non-empty page.

The panel is shared, so one officer changing the page changes it for
everyone looking at that message. Each officer's dropdown *selection*
remains private — that is already handled by `DistributePanel.selected`
being keyed on user id — so no officer can approve another's pick. The
page flip is a display annoyance, not a correctness bug, and is accepted
in exchange for keeping a single shared view of what is pending.

## Testing

Test-first, extending `tests/test_items_state.py` and
`tests/test_items_bot.py`:

- a 50-request queue plus a 50-member IGN map round-trips through
  encode/decode with every request intact and in order
- a state message in the old single-object format still decodes
- shards decode correctly when supplied out of order
- a queue at `MAX_SHARDS` refuses a new request and leaves the existing
  queue untouched
- `save_state` deletes surplus shard messages when the queue drains
- `load_state` with a missing part restores the rest and reports the gap
- page buttons render for a multi-page queue and redraw the correct
  slice; a single-page queue renders no page buttons
- approving the last request on the final page falls back to a
  non-empty page

## Out of scope

Bounding the growth of the `igns` map, moving any state to the
spreadsheet, and any change to the eligibility rules in
`items_rules.py`.
