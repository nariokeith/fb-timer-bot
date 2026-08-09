# A public queue board for the member channel

Date: 2026-08-09

## The idea

Members can see their own pending requests with `!myrequests`, but they
cannot see the queue. Only officers can, through `!distribute` in the
private officer channel. The request is for the Discord equivalent of the
LED queue board hanging above a service counter: a pinned message in the
member channel showing who is waiting, for what, and in what order.

Three columns, and only three: position, IGN, item.

## Design

### Rendering: a new `items_board.py`

`items_bot.py` is 938 lines. The board's rendering goes in its own module,
pure and free of Discord and the network, so the layout can be tested
directly the way `items_rules.py` and `items_state.py` are.

The board renders as a fenced code block, because a proportional font
will not hold columns in line:

```
 #   IGN              ITEM
 1   Odaiba           Dark Orb Earrings
 2   Kobe             Asta's Heart
 3   Dajz             Benji's Heart

 +12 more waiting
```

`BOARD_LIMIT = 30` rows. A board is read at a glance on a phone; past
thirty rows it stops being glanceable, and anyone further down still
learns their position from the `+N more waiting` line and from
`!myrequests`.

IGN and item are truncated to fixed widths so that one long item name
cannot wrap and destroy the alignment of every row beneath it.

Position 1 is the oldest waiting request. The number is a position, not a
ticket: it shifts as the queue moves. Because officers may approve out of
order, a member can watch their position jump. That is an honest
reflection of what is happening and is preferred to a fixed ticket
number, which would develop gaps and stop answering "how many are ahead
of me".

### State: two new fields

`State` gains `queue_channel_id: int | None` and
`board_message_id: int | None`, written into shard 0 beside
`officer_channel_id`. Both are absent from the messages pinned in
production today and decode as `None`, exactly as the sharding change
handled its own schema change.

The message id must be stored, not just the channel: after a restart the
bot has to edit the board it already pinned rather than pin a second one.

### `!setqueuechannel`

Administrator-only, run in the channel members should watch. It records
the channel, posts and pins the board, and deletes any previous board so
that moving the board never leaves a frozen copy behind in the old
channel.

### Refresh

`refresh_board()` runs after every change to the queue: request,
approval, denial, cancellation. One message edit per action, which is
affordable now that a save costs one edit rather than six.

**The board is cosmetic and must never break anything real.** Every board
operation is wrapped so that a failure to render, edit, or repost it
cannot fail a member's request, and above all cannot abort an approval
*after* the spreadsheet has already been written. Failures go to the log
and the next change repairs the board.

### What the board deliberately omits

No eligibility flags, no officer notes, no daily-cap counts, no Discord
mentions. Those are officer-facing judgments and stay in `!distribute`.
Nothing on the board reveals whether a member is over their cap or has
been flagged.

## Testing

`tests/test_items_board.py`, for the pure rendering:

- columns stay aligned when IGNs and item names vary in length
- an over-long IGN and an over-long item name are truncated, not wrapped
- exactly `BOARD_LIMIT` rows shows no `+N more` line; one past it shows
  `+1 more waiting`
- an empty queue renders a "nothing pending" board rather than an empty
  code block
- numbering starts at 1 and is continuous

`tests/test_items_bot.py`, for the wiring:

- the board is refreshed on request, approve, deny and cancel
- a board message that has been deleted is reposted and re-pinned
- `!setqueuechannel` in a new channel deletes the previous board
- a board failure does not prevent a request being queued
- a board failure does not prevent an approval completing — the
  regression that would otherwise write to the sheet and then throw

## Out of scope

Restricting `!request` to the board channel, a "recently approved" tail,
an item-type column, and a last-updated footer. All were considered and
declined.

## Addendum: reposting the board so it follows the chat

Editing a Discord message never moves it, so the board kept its original
position and scrolled out of view as the channel filled. Members had to
open the pinned-messages list to find it, which defeats the point of a
board you glance at.

A true fixed banner is not something Discord offers a bot. Of the
approximations -- a dedicated read-only channel, the channel topic, or
reposting -- reposting was chosen: the guild already has too many
channels, and the channel topic is capped at 1024 characters, collapses
to one line, and is rate-limited far below the refresh rate.

Every `BOARD_REPOST_EVERY = 5` successful requests, `refresh_board`
deletes the board and posts a fresh one at the bottom of the channel
instead of editing in place. Only successful requests count: a refused
request does not change the board, and approvals, denials and
cancellations keep editing in place so working the queue down does not
spray new messages.

The counter is in memory rather than in the saved state. A restart
resetting it to zero costs at most one late repost, which is not worth
another field in a state schema that has to stay backward compatible.

If the delete succeeds but the send fails, `board_message_id` is cleared
so the next refresh posts a fresh board rather than editing a message
that no longer exists.
