"""The bot's state, stored as JSON in a pinned Discord message.

Render's free tier restarts on every deploy and can spin down, so an
in-memory queue silently loses pending requests. A tab in the
spreadsheet would work but turns every !request into a Sheets write on
the member-facing path. A pinned message costs nothing extra and is the
same mechanism bot.py already uses in production on this instance
(FBTIMER_STATE_V1).

Pure module: it produces and consumes strings. Reading and writing the
Discord message is items_bot's job.
"""

import dataclasses
import json
import secrets
from dataclasses import dataclass, field

import items_rules

STATE_MARKER = "ITEMS_STATE_V1"

# Discord's hard limit is 2000 characters. The margin absorbs the
# marker line and the fence, exactly as bot.py's encode_state does.
MAX_CONTENT = 1990

# Twenty pinned messages, against Discord's limit of 50 per channel. The
# ceiling exists so a runaway queue cannot bury the channel, not because
# the API is near its own -- and save_state rewrites only the shards
# whose contents actually changed, so a high count is not a high cost.
MAX_SHARDS = 20


@dataclass(frozen=True)
class PendingRequest:
    id: str
    user_id: int
    ign: str
    item: str
    type: str
    requested_at: str
    # Something the officer should see when judging this request, e.g.
    # that the member has previously requested under a different IGN.
    # Empty for the ordinary case.
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "ign": self.ign,
            "item": self.item,
            "type": self.type,
            "requested_at": self.requested_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PendingRequest":
        return cls(
            id=str(raw["id"]),
            user_id=int(raw["user_id"]),
            ign=str(raw["ign"]),
            item=str(raw["item"]),
            type=str(raw["type"]),
            requested_at=str(raw["requested_at"]),
            # Absent in messages written before notes existed.
            note=str(raw.get("note", "")),
        )


# A listed raffle carries an eligible IGN for every voter, so raffles are
# the bulkiest thing in the state. Twenty-five covers a guild raffling
# twenty items in one day with headroom, and measures at 15 shards even
# when every one is listed with a full roster -- see the fits() test.
MAX_RAFFLES = 25


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
            # Written for an older bot that may read this pin after a
            # rollback: it understands only a single winner, and without
            # this it would read a drawn raffle as undrawn and supersede it.
            "winner": self.winners[0] if self.winners else "",
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


@dataclass
class State:
    officer_channel_id: int | None = None
    queue_channel_id: int | None = None
    board_message_id: int | None = None
    queue: list[PendingRequest] = field(default_factory=list)
    # Discord user id (as a string, because JSON object keys are strings)
    # -> the IGN they last used. Members type their own IGN, so this is
    # what lets a typo surface as "you used Kobe before" instead of
    # silently crediting a different row.
    igns: dict[str, str] = field(default_factory=dict)
    # Discord id -> the IGN this account IS. Set deliberately by !iam or
    # !bind, unlike `igns` above, which only records what the account last
    # requested under and may name an alt.
    bindings: dict[str, str] = field(default_factory=dict)
    # Discord ids known to have no roster row at all -- guests and former
    # members. Without this they would block every raffle freeze forever.
    not_players: list[str] = field(default_factory=list)
    # Roles permitted to run !poll / !list / !winner, and the one channel
    # they work in. Unlike !distribute -- which is authorised by the
    # officer channel itself -- the raffle happens in a member-visible
    # channel, so the channel cannot also be the permission.
    raffle_role_ids: list[int] = field(default_factory=list)
    raffle_channel_id: int | None = None
    raffles: list["Raffle"] = field(default_factory=list)
    # A partial pin read can still restore requests, but the bot needs to
    # warn officers that it could not recover the complete queue.
    missing_parts: tuple[int, ...] = ()


@dataclass(frozen=True)
class Shard:
    part: int
    total: int
    state: State


def new_request_id() -> str:
    """A short token identifying one request.

    Written to the ledger and used to detect two officers resolving the
    same request: the second finds it already gone from the queue.
    """
    return secrets.token_hex(4)


def _render(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{STATE_MARKER} -- bot storage, please don't delete this message.\n"
        f"```json\n{body}\n```"
    )


def _encode_with_total(state: State, total: int) -> list[str]:
    first_payload = {
        "part": 0,
        "total": total,
        "officer_channel_id": state.officer_channel_id,
        "igns": {},
        "queue": [],
    }
    if state.queue_channel_id is not None:
        first_payload["queue_channel_id"] = state.queue_channel_id
    if state.board_message_id is not None:
        first_payload["board_message_id"] = state.board_message_id
    if state.raffle_channel_id is not None:
        first_payload["raffle_channel_id"] = state.raffle_channel_id
    if state.raffle_role_ids:
        first_payload["raffle_role_ids"] = list(state.raffle_role_ids)
    if state.not_players:
        first_payload["not_players"] = list(state.not_players)
    first_payload["raffles"] = []
    payloads = [first_payload]

    for user_id, ign in state.igns.items():
        current = payloads[-1]
        current.setdefault("igns", {})[user_id] = ign
        if len(_render(current)) <= MAX_CONTENT:
            continue

        current["igns"].pop(user_id)
        current = {"part": len(payloads), "total": total, "igns": {}, "queue": []}
        payloads.append(current)
        current["igns"][user_id] = ign
        if len(_render(current)) > MAX_CONTENT:
            raise ValueError("a remembered IGN is too large for a state shard")

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

    for request in state.queue:
        request_payload = request.to_dict()
        current = payloads[-1]
        current["queue"].append(request_payload)
        if len(_render(current)) <= MAX_CONTENT:
            continue

        current["queue"].pop()
        current = {"part": len(payloads), "total": total, "queue": []}
        payloads.append(current)

        current["queue"].append(request_payload)
        if len(_render(current)) > MAX_CONTENT:
            raise ValueError("a pending request is too large for a state shard")

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

    contents = [_render(payload) for payload in payloads]
    # The loops above bound the shards they fill item by item, but shard 0
    # also carries whole-state fields -- the raffle role ids especially --
    # that no loop measures. An oversized shard is not a shard Discord will
    # accept, and save_state may already have deleted the message it was
    # replacing, so this must be caught here where fits() can see it rather
    # than mid-write.
    for content in contents:
        if len(content) > MAX_CONTENT:
            raise ValueError(
                "a state shard is too large to store; the configuration in "
                "shard 0 does not fit in one Discord message"
            )
    return contents


def encode_state(state: State) -> list[str]:
    """Render the state into self-contained Discord message shards.

    Nothing is ever dropped: a queue too big for one message spills into
    another shard rather than losing the member who has waited longest.
    Callers ask `fits` first and refuse the new request when it says no.
    """
    total = 1
    # Only the decimal width of ``total`` can change packing. A fixed
    # ceiling turns an unexpected non-converging packer into a clear error.
    for _ in range(100):
        contents = _encode_with_total(state, total)
        if len(contents) == total:
            return contents
        total = len(contents)
    raise ValueError("state shard count did not stabilize")


def fits(state: State) -> bool:
    """Whether this state can be saved without exceeding the shard limit."""
    try:
        return len(encode_state(state)) <= MAX_SHARDS
    except ValueError:
        return False


def decode_state(content: str) -> Shard | None:
    """Parse a state message, or None if this isn't one / is corrupt.

    Returning None rather than raising lets the caller scan a channel's
    pins and skip anything that isn't ours, the way bot.restore_state
    does.
    """
    if not content or not content.startswith(STATE_MARKER):
        return None
    start = content.find("```json")
    end = content.rfind("```")
    if start == -1 or end <= start:
        return None
    body = content[start + len("```json") : end].strip()
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None
        queue = [PendingRequest.from_dict(r) for r in payload.get("queue", [])]
        channel_id = payload.get("officer_channel_id")
        channel_id = int(channel_id) if channel_id is not None else None
        queue_channel_id = payload.get("queue_channel_id")
        queue_channel_id = (
            int(queue_channel_id) if queue_channel_id is not None else None
        )
        board_message_id = payload.get("board_message_id")
        board_message_id = int(board_message_id) if board_message_id is not None else None
        igns = {str(k): str(v) for k, v in dict(payload.get("igns", {})).items()}
        bindings = {
            str(k): str(v) for k, v in dict(payload.get("bindings", {})).items()
        }
        not_players = [str(u) for u in payload.get("not_players", [])]
        raffles = [Raffle.from_dict(r) for r in payload.get("raffles", [])]
        raffle_channel_id = payload.get("raffle_channel_id")
        raffle_channel_id = (
            int(raffle_channel_id) if raffle_channel_id is not None else None
        )
        raffle_role_ids = [int(r) for r in payload.get("raffle_role_ids", [])]
        part = int(payload.get("part", 0))
        total = int(payload.get("total", 1))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if part < 0 or total < 1 or part >= total:
        return None
    return Shard(
        part=part,
        total=total,
        state=State(
            officer_channel_id=channel_id,
            queue_channel_id=queue_channel_id,
            board_message_id=board_message_id,
            queue=queue,
            igns=igns,
            bindings=bindings,
            not_players=not_players,
            raffle_role_ids=raffle_role_ids,
            raffle_channel_id=raffle_channel_id,
            raffles=raffles,
        ),
    )


def decode_shards(contents: list[str]) -> State | None:
    """Restore every readable state shard, retaining any partial recovery."""
    shards = [shard for content in contents if (shard := decode_state(content))]
    if not shards:
        return None

    shards.sort(key=lambda shard: shard.part)
    missing_parts = tuple(
        part
        for part in range(max(shard.total for shard in shards))
        if part not in {shard.part for shard in shards}
    )
    officer_channel_id = next(
        (
            shard.state.officer_channel_id
            for shard in shards
            if shard.state.officer_channel_id is not None
        ),
        None,
    )
    queue_channel_id = next(
        (
            shard.state.queue_channel_id
            for shard in shards
            if shard.state.queue_channel_id is not None
        ),
        None,
    )
    board_message_id = next(
        (
            shard.state.board_message_id
            for shard in shards
            if shard.state.board_message_id is not None
        ),
        None,
    )
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
    igns: dict[str, str] = {}
    bindings: dict[str, str] = {}
    not_players: list[str] = []
    queue: list[PendingRequest] = []
    raffles: list[Raffle] = []
    for shard in shards:
        igns.update(shard.state.igns)
        bindings.update(shard.state.bindings)
        for user_id in shard.state.not_players:
            # Shard 0 carries the list, but a re-sharded pin can repeat it.
            if user_id not in not_players:
                not_players.append(user_id)
        queue.extend(shard.state.queue)
        raffles.extend(shard.state.raffles)
    return State(
        officer_channel_id=officer_channel_id,
        queue_channel_id=queue_channel_id,
        board_message_id=board_message_id,
        queue=queue,
        igns=igns,
        bindings=bindings,
        not_players=not_players,
        raffle_role_ids=raffle_role_ids,
        raffle_channel_id=raffle_channel_id,
        raffles=raffles,
        missing_parts=missing_parts,
    )


def pending_gear_for(state: State, ign: str, today: str) -> int:
    """Queued-but-unapproved gear requests for this player."""
    wanted = items_rules.normalize(ign)
    return sum(
        1
        for r in state.queue
        if (
            r.type == items_rules.GEAR
            and items_rules.normalize(r.ign) == wanted
            and items_rules.pht_day(r.requested_at) == today
        )
    )


def find_request(state: State, request_id: str) -> PendingRequest | None:
    for request in state.queue:
        if request.id == request_id:
            return request
    return None


def remove_request(state: State, request_id: str) -> PendingRequest | None:
    """Take a request out of the queue, returning it.

    None means it was already resolved -- which is exactly how a second
    officer clicking the same button is detected.
    """
    found = find_request(state, request_id)
    if found is not None:
        state.queue.remove(found)
    return found


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

    Only a raffle that has been DRAWN may be dropped -- `now` is unused
    and kept for callers, because a poll's clock is not what makes a
    raffle finished. An ended-but-undrawn raffle still holds the frozen
    eligible pool that !winner checks against, and it is the only copy:
    the poll message may be gone, and Discord will not recompute it.
    Dropping one to make room would destroy the record of who was
    eligible without anyone being told.

    So a full state refuses, and the officer clears it by drawing a
    winner -- which is the thing they were going to do anyway.
    """
    allowed, victim = raffle_to_evict(state)
    if victim is not None:
        state.raffles.remove(victim)
    return allowed


def raffle_to_evict(state: State) -> tuple[bool, Raffle | None]:
    """(is there room, which raffle pays for it) -- without removing it.

    Separate from evict_for_new_raffle because the caller must know the
    price before posting a poll: a poll that Discord rejects must not
    have cost a slot, and once posted it cannot be untaken.

    A partly drawn raffle is also unevictable, because its remaining names
    still have to be recorded.
    """
    if len(state.raffles) < MAX_RAFFLES:
        return True, None
    drawn = [raffle for raffle in state.raffles if raffle.drawn]
    if not drawn:
        return False, None
    return True, min(drawn, key=lambda raffle: raffle.created_at)
