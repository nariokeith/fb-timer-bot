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
    queue: list[PendingRequest] = field(default_factory=list)
    # Discord user id (as a string, because JSON object keys are strings)
    # -> the IGN they last used. Members type their own IGN, so this is
    # what lets a typo surface as "you used Kobe before" instead of
    # silently crediting a different row.
    igns: dict[str, str] = field(default_factory=dict)


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


def encode_state(state: State) -> tuple[str, list[PendingRequest]]:
    """Render the state, dropping the oldest requests if it will not fit.

    Returns (content, dropped). The caller MUST tell the officers about
    anything dropped -- silently losing a member's request is the one
    failure mode this whole module exists to prevent, so it is surfaced
    loudly rather than swallowed.
    """
    queue = list(state.queue)
    dropped: list[PendingRequest] = []

    while True:
        content = _render(
            {
                "officer_channel_id": state.officer_channel_id,
                "queue": [r.to_dict() for r in queue],
                "igns": state.igns,
            }
        )
        if len(content) <= MAX_CONTENT or not queue:
            return content, dropped
        dropped.append(queue.pop(0))


def decode_state(content: str) -> State | None:
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
        queue = [PendingRequest.from_dict(r) for r in payload.get("queue", [])]
        channel_id = payload.get("officer_channel_id")
        igns = {str(k): str(v) for k, v in dict(payload.get("igns", {})).items()}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return State(
        officer_channel_id=int(channel_id) if channel_id is not None else None,
        queue=queue,
        igns=igns,
    )


def pending_gear_for(state: State, ign: str) -> int:
    """Queued-but-unapproved gear requests for this player."""
    wanted = items_rules.normalize(ign)
    return sum(
        1
        for r in state.queue
        if r.type == items_rules.GEAR and items_rules.normalize(r.ign) == wanted
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
