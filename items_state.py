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

import json
import secrets
from dataclasses import dataclass, field

import items_rules

STATE_MARKER = "ITEMS_STATE_V1"

# Discord's hard limit is 2000 characters. The margin absorbs the
# marker line and the fence, exactly as bot.py's encode_state does.
MAX_CONTENT = 1990
MAX_SHARDS = 10


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

    return [_render(payload) for payload in payloads]


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
    igns: dict[str, str] = {}
    queue: list[PendingRequest] = []
    for shard in shards:
        igns.update(shard.state.igns)
        queue.extend(shard.state.queue)
    return State(
        officer_channel_id=officer_channel_id,
        queue_channel_id=queue_channel_id,
        board_message_id=board_message_id,
        queue=queue,
        igns=igns,
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
