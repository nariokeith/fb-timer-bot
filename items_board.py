"""Render the member-facing pending-request queue.

Pure module: Discord presents the result, while this module makes the
queue layout from values it is given.
"""

import re

import items_state

BOARD_LIMIT = 30
IGN_WIDTH = 16
ITEM_WIDTH = 30


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def _display(value: str, width: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return _truncate(value.replace("`", "′"), width)


def render_board(queue: list[items_state.PendingRequest]) -> str:
    """Render the current queue as a fixed-width code block."""
    if not queue:
        return "```\nNothing pending.\n```"
    lines = [f"{'#':>2}   {'IGN':<{IGN_WIDTH}} ITEM"]
    for number, request in enumerate(queue[:BOARD_LIMIT], start=1):
        ign = _display(request.ign, IGN_WIDTH)
        item = _display(request.item, ITEM_WIDTH)
        lines.append(f"{number:>2}   {ign:<{IGN_WIDTH}} {item}")
    remaining = len(queue) - BOARD_LIMIT
    if remaining > 0:
        lines.extend(("", f"+{remaining} more waiting"))
    return "```\n" + "\n".join(lines) + "\n```"
