"""Match names read off a screenshot against the players already in the sheet.

The bot never invents a player row. Every raw string either resolves to a
name already in column A, or it is reported as unmatched for a human to
sort out. That constraint is what lets a free OCR engine be good enough --
this is matching against ~35 known strings, not open transcription.

In-game names sometimes carry suffixes (like "KobePH") that the sheet does
not. The ALIASES table maps these in-game variants to exact sheet rows, and
is maintained by hand as the guild's roster changes. An alias resolves only
if its target row actually exists in the sheet.
"""

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# A single-character misread in an 11-character name scores about 0.909,
# so 0.85 accepts realistic OCR noise. Unrelated names top out near 0.53.
MATCH_THRESHOLD = 0.85

# The best candidate must beat the runner-up by this much. Without it,
# "Kobe0" against "Kobe01" and "Kobe02" (both 0.909) would award points
# to whichever happened to sort first.
AMBIGUITY_MARGIN = 0.05

# In-game names that differ from the sheet's row for the same person.
# Keys are what the OCR reads; values are the exact sheet row.
# Some in-game names share no characters with their sheet row (e.g. a Japanese
# in-game name and a Filipino nickname in Latin script). Aliases are the ONLY
# resolution path for these cases -- no fuzzy threshold, however low, could ever
# match them. Fuzzy matching is not an alternative here.
ALIASES = {
    "KobePH": "Kobe",
    "面白い": "chinchong ni Mumu",
}


class DuplicatePlayerName(RuntimeError):
    """Two known players normalise to the same string.

    match_names' index is built from normalize(known_player), which
    casefolds. A sheet holding both "Kobe" and "kobe" would otherwise let
    the second silently overwrite the first in that index -- the sheet's
    own duplicate-row check (attendance_sheet._rows_by_player) does not
    catch this, because "Kobe" != "kobe" as exact strings, so it never
    sees a collision. The result: one of the two players can never be
    matched again, and the OCR'd name that should have gone to them
    silently resolves to the other player instead. Refusing here, naming
    both, matches the refuse-rather-than-guess convention used throughout
    this bot rather than picking a winner.
    """


@dataclass(frozen=True)
class Match:
    raw: str
    player: str
    score: float


def normalize(name: str) -> str:
    """Casefold and collapse whitespace, preserving non-ASCII characters.

    NFKC rather than stripping to ASCII, because real player names contain
    characters like the one in "wileKAMOTE卐".
    """
    folded = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(folded.split())


def _score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _best_candidate(target: str, index: dict[str, str]) -> tuple[str, float] | None:
    """Highest-scoring player for `target`, or None if unclear."""
    scored = sorted(
        ((_score(target, key), player) for key, player in index.items()),
        reverse=True,
    )
    if not scored:
        return None

    best_score, best_player = scored[0]
    if best_score < MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and best_score - scored[1][0] < AMBIGUITY_MARGIN:
        return None
    return best_player, best_score


def match_names(
    raw_names: list[str], known_players: list[str]
) -> tuple[list[Match], list[str]]:
    """Resolve raw strings to known players.

    Returns (matched, unmatched). Matches are deduplicated by resolved
    player, so a name the model reads twice never earns double points.

    Raises DuplicatePlayerName if two DIFFERENT known players normalise to
    the same string (e.g. "Kobe" and "kobe") -- see that class for why.
    """
    index: dict[str, str] = {}
    for player in known_players:
        if not player.strip():
            continue
        key = normalize(player)
        if key in index and index[key] != player:
            raise DuplicatePlayerName(
                f"{index[key]!r} and {player!r} both normalise to the same "
                f"name ({key!r}); refusing to guess which one a matched "
                "screenshot name means"
            )
        index[key] = player
    alias_index = {
        normalize(alias_raw): target
        for alias_raw, target in ALIASES.items()
        if normalize(target) in index
    }

    matched: list[Match] = []
    unmatched: list[str] = []
    seen: set[str] = set()

    for raw in raw_names:
        target = normalize(raw)
        if not target:
            continue

        if target in index:
            player, score = index[target], 1.0
        elif target in alias_index:
            player, score = index[normalize(alias_index[target])], 1.0
        else:
            candidate = _best_candidate(target, index)
            if candidate is None:
                unmatched.append(raw)
                continue
            player, score = candidate

        if player in seen:
            continue
        seen.add(player)
        matched.append(Match(raw=raw, player=player, score=score))

    return matched, unmatched
