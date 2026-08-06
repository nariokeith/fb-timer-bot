"""Read player names off a roster screenshot using the Gemini free tier.

Uses the Interactions API (client.interactions.create), the current Gemini
SDK surface -- not the older models.generate_content. The reply is
constrained by a JSON schema so the model cannot answer with prose that
would need parsing.
"""

import base64
import json
import os

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "names": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["names"],
}

# Validated against a real Manage Rally screenshot from this guild on
# 2026-08-06: 4/4 runs correctly excluded the one dimmed player, and 3/3
# runs on a crop containing no dimmed players kept all ten -- so it does
# not over-exclude. The earlier, looser wording included the dimmed
# player 4/4 times.
PROMPT = (
    "This image is a roster panel from the mobile game Lordnine: Infinite "
    "Class. It may be a party list, a guild member list, or a rally / "
    "squad management screen.\n\n"
    "List the player character names that are shown as ACTIVE, and only "
    "those. Copy each name exactly as written, preserving capitalisation, "
    "spacing, punctuation and any non-Latin characters.\n\n"
    "Exclude a name if it is rendered dimmed, greyed out, faded, or at "
    "lower contrast than the other names around it. In this game's "
    "interface a dimmed entry means that player is not confirmed present, "
    "so it must not be listed. Compare the names against each other: the "
    "active ones share the same bright text colour, and a dimmed one is "
    "visibly darker or washed out.\n\n"
    "Also ignore: character levels, class names and icons, guild ranks "
    "and tags, HP and MP bars, damage numbers, timers, currency amounts, "
    "buttons, tab labels, chat text, the boss or monster name, and every "
    "other interface label.\n\n"
    "If the same player appears more than once (for example in both a "
    "side panel and a main grid), list them only once."
)

# Free tier: 15 requests/minute, 1,000/day -- far above expected volume.
# If accuracy proves insufficient, set GEMINI_MODEL=gemini-3.5-flash
# (10/min, 250/day), also free.
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


class VisionError(RuntimeError):
    """The screenshot could not be turned into a list of names."""


def _new_client():
    from google import genai

    return genai.Client()


def extract_names(image_bytes: bytes, mime_type: str, *, client=None) -> list[str]:
    """Return the player names visible in a roster screenshot.

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

    names = payload.get("names") if isinstance(payload, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise VisionError(f"Gemini reply had an unexpected shape: {payload!r}")

    cleaned = [n.strip() for n in names if n.strip()]
    if not cleaned:
        raise VisionError("Gemini found no names in that image")
    return cleaned
