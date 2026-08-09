"""Read player names off a roster screenshot using the Gemini free tier.

Uses the Interactions API (client.interactions.create), the current Gemini
SDK surface -- not the older models.generate_content. The reply is
constrained by a JSON schema so the model cannot answer with prose that
would need parsing.
"""

import base64
import json
import os
from dataclasses import dataclass

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "players": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "dimmed"]},
                },
                "required": ["name", "status"],
            },
        },
    },
    "required": ["players"],
}

# The classify-every-name wording and foreground-panel-only rule have not
# yet been validated against a live screenshot. The preview's Skipped field
# makes a misclassification visible to an officer before points are written.
PROMPT = (
    "This image is a roster panel from the mobile game Lordnine: Infinite "
    "Class. It may be a party list, a guild member list, or a rally / "
    "squad management screen.\n\n"
    "List EVERY visible player character name exactly once. For each one, "
    "set status to exactly active or dimmed. Never omit a player name "
    "because it is dimmed; classify it as dimmed instead. Copy each name "
    "exactly as written, preserving capitalisation, spacing, punctuation "
    "and any non-Latin characters.\n\n"
    "If a dialog or modal panel is open in the foreground, for example "
    "Manage Rally, list ONLY the player names inside that foreground panel. "
    "Ignore everything behind or outside it entirely, including side party "
    "lists, floating nameplates over the game world, and chat lines. When "
    "no dialog is open, read the whole roster panel.\n\n"
    "A dimmed entry is darker, greyed out, faded, washed out, or lower "
    "contrast than the names around it. In this game's interface it means "
    "that player is not confirmed present. Compare the names against each "
    "other: active names share the same bright text colour.\n\n"
    "Also ignore: character levels, class names and icons, guild ranks "
    "and tags, HP and MP bars, damage numbers, timers, currency amounts, "
    "buttons, tab labels, chat text, the boss or monster name, and every "
    "other interface label.\n\n"
    "If the same player appears more than once (for example in both a "
    "side panel and a main grid), list them only once."
)

# Free tier: 10 requests/minute, 250/day. This stronger vision tier is
# deliberate because deciding whether low-contrast text is dimmed needs
# more reliable visual judgement than the lighter model provides.
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")


class VisionError(RuntimeError):
    """The screenshot could not be turned into a list of names."""


@dataclass(frozen=True)
class RosterRead:
    """The active and dimmed player names the model reported, in reply order."""

    active: list[str]
    dimmed: list[str]


def _new_client():
    from google import genai

    return genai.Client()


def read_roster(image_bytes: bytes, mime_type: str, *, client=None) -> RosterRead:
    """Return the active and dimmed names visible in a roster screenshot.

    `client` is injectable so tests never touch the network. Raises
    VisionError for anything that is not a usable list of names.
    """
    client = client or _new_client()

    try:
        interaction = client.interactions.create(
            model=MODEL,
            input=[
                {"type": "text", "text": PROMPT},
                {
                    "type": "image",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                    "mime_type": mime_type,
                },
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": RESPONSE_SCHEMA,
            },
        )
    except Exception as exc:  # SDK raises assorted transport/quota errors
        raise VisionError(f"Gemini request failed: {exc}") from exc

    raw = interaction.output_text
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise VisionError(f"Gemini reply was not valid JSON: {raw!r}") from exc

    players = payload.get("players") if isinstance(payload, dict) else None
    if not isinstance(players, list) or not all(
        isinstance(player, dict) for player in players
    ):
        raise VisionError(f"Gemini reply had an unexpected shape: {payload!r}")

    active: list[str] = []
    dimmed: list[str] = []
    for player in players:
        name, status = player.get("name"), player.get("status")
        # Casing and surrounding whitespace are normalised, but an
        # unrecognised word still refuses. The distinction matters: a
        # status nobody can interpret must never be guessed at, because
        # guessing "active" is what silently pays a player who was not
        # there. "Active" is not ambiguous, though -- and since an
        # unrecognised status aborts the whole command, treating a
        # capital letter as unreadable would take attendance logging
        # down entirely over a reply whose meaning is plain. The
        # schema's enum should make this moot; it is defence against
        # structured output being enforced less strictly by whatever
        # model GEMINI_MODEL happens to name.
        if isinstance(status, str):
            status = status.strip().casefold()
        if (
            not isinstance(name, str)
            or not isinstance(status, str)
            or status not in {"active", "dimmed"}
        ):
            raise VisionError(f"Gemini reply had an unexpected shape: {payload!r}")
        name = name.strip()
        if not name:
            continue
        if status == "active":
            active.append(name)
        else:
            dimmed.append(name)

    if not active:
        if dimmed:
            raise VisionError(
                "Gemini found no active names; every name in that image "
                "was dimmed"
            )
        raise VisionError("Gemini found no names in that image")
    return RosterRead(active=active, dimmed=dimmed)
