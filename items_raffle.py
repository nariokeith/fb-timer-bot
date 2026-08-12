"""Pure logic for the special log raffle.

No Discord, no Google Sheets, no clock. The bot passes in the voters it
fetched and the sheet it read; everything decided here is decided from
those values, which is what makes the nickname rules testable without a
network.

The hard part is identity. Discord gives the bot a nickname; the sheet
is keyed by IGN. Nicknames carry a guild tag ("BK | Jjew") in several
formats, and the roster contains multi-word rows ("chinchong ni Mumu")
and hyphenated rows ("wile-KAMOTE"), so neither "take the last word" nor
"split on separators and re-join" can work.
"""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import get_close_matches

from attendance_roster import normalize
import items_rules

# Characters that can sit between a guild tag and the IGN. Whitespace is
# handled separately because it is also a legitimate part of an IGN.
SEPARATORS = "|-:/"


def nickname_candidates(nickname: str) -> list[str]:
    """Every substring of the nickname that might be the IGN.

    The whole nickname first, then the remainder after each separator.
    Each remainder is a SLICE of the original string rather than a
    re-join of split tokens: re-joining would have to reconstruct the
    internal spacing of 'chinchong ni Mumu' and would corrupt any IGN
    containing one of the separator characters.
    """
    text = " ".join(nickname.split())
    if not text:
        return []

    candidates = [text]
    for index, character in enumerate(text):
        if character not in SEPARATORS and not character.isspace():
            continue
        remainder = text[index + 1 :].lstrip(SEPARATORS + " \t")
        if remainder and remainder not in candidates:
            candidates.append(remainder)
    return candidates


def resolve_voter(nickname: str, roster: list[str]) -> str | None:
    """The roster row this nickname belongs to, or None.

    None when nothing matches AND when two candidates match two
    different rows. An ambiguous nickname is reported to an officer
    rather than guessed: the consequence of a wrong answer here is a
    permanently ticked checkbox on the wrong player.
    """
    matched: list[str] = []
    for candidate in nickname_candidates(nickname):
        try:
            player = items_rules.resolve_ign(candidate, roster)
        except items_rules.RequestParseError:
            # Two roster rows normalise identically. That is a sheet
            # problem, not this voter's; nobody can be resolved safely.
            return None
        if player is not None and player not in matched:
            matched.append(player)

    return matched[0] if len(matched) == 1 else None


@dataclass(frozen=True)
class Voter:
    """One Discord account that answered the poll.

    display_name is the server nickname when the bot could see the
    member, and the account's global name otherwise. user_id is carried
    so an unidentified voter can be named by mention in the reply --
    a nickname alone would not let an officer find them.
    """

    user_id: int
    display_name: str


@dataclass(frozen=True)
class VoterSplit:
    eligible: list[str] = field(default_factory=list)
    already_have: list[str] = field(default_factory=list)
    unidentified: list[Voter] = field(default_factory=list)


def classify_voters(
    voters: list[Voter],
    roster: list[str],
    holds: Callable[[str], bool],
) -> VoterSplit:
    """Split the poll's voters into the three groups an officer needs.

    `holds` answers "is this player's checkbox already ticked for this
    special log". It is passed in rather than read here so this stays
    pure: the bot binds it to the snapshot it already read.

    Duplicates are collapsed by resolved IGN, so a member voting from an
    alt account cannot appear in the pool twice and double their odds.
    Order is the order they voted in -- stable, and visibly not shuffled
    by the bot, since the draw itself is done by a human.
    """
    eligible: list[str] = []
    already_have: list[str] = []
    unidentified: list[Voter] = []
    seen: set[str] = set()

    for voter in voters:
        player = resolve_voter(voter.display_name, roster)
        if player is None:
            unidentified.append(voter)
            continue
        key = normalize(player)
        if key in seen:
            continue
        seen.add(key)
        (already_have if holds(player) else eligible).append(player)

    return VoterSplit(
        eligible=eligible, already_have=already_have, unidentified=unidentified
    )

DEFAULT_POLL_HOURS = 24
MIN_POLL_HOURS = 1
# Discord allows longer, but a week is already far past any real raffle
# and a bounded value can never be rejected by the API mid-command.
MAX_POLL_HOURS = 168

HOURS_FLAG = "--hours"

POLL_USAGE = "Usage: `!poll <special log name> [--hours N]`"
WINNER_USAGE = (
    "Usage: `!winner <special log name> <IGN>`, or "
    "`!winner <special log name> <IGN> - <IGN> - <IGN>` for several winners."
)


class RaffleArgumentError(RuntimeError):
    """A raffle command's argument does not resolve. Message is user-facing."""


@dataclass(frozen=True)
class PollArgument:
    item_query: str
    hours: int


def parse_poll_argument(argument: str) -> PollArgument:
    """Split '<item name> [--hours N]'.

    The flag is trailing because the item name is multi-word and
    unquoted, so a leading flag would be indistinguishable from the
    first word of a name. '--hours' cannot occur inside a sheet header.
    """
    words = argument.split()
    hours = DEFAULT_POLL_HOURS

    if HOURS_FLAG in words:
        index = words.index(HOURS_FLAG)
        value = words[index + 1 :]
        if len(value) != 1:
            raise RaffleArgumentError(
                f"`{HOURS_FLAG}` takes exactly one number and must come last. "
                f"{POLL_USAGE}"
            )
        try:
            hours = int(value[0])
        except ValueError:
            raise RaffleArgumentError(
                f"`{HOURS_FLAG} {value[0]}` is not a whole number of hours."
            ) from None
        if not MIN_POLL_HOURS <= hours <= MAX_POLL_HOURS:
            raise RaffleArgumentError(
                f"A poll must run between {MIN_POLL_HOURS} and "
                f"{MAX_POLL_HOURS} hours."
            )
        words = words[:index]

    item_query = " ".join(words)
    if not item_query:
        raise RaffleArgumentError(f"Which special log? {POLL_USAGE}")
    return PollArgument(item_query=item_query, hours=hours)


def split_item_and_ign(
    argument: str, item_names: list[str], roster: list[str]
) -> tuple[str, str]:
    """Split '<item name> <IGN>' where BOTH parts may contain spaces.

    Every split point is tried; a reading is accepted only when the
    prefix names one of `item_names` and the suffix resolves to a roster
    row. Two valid readings is a refusal, not a coin toss -- the write
    this feeds is a permanent checkbox.

    Ascending split index tries the LONGEST IGN first, which is what
    lets 'chinchong ni Mumu' win over a shorter player name inside it.
    """
    words = argument.split()
    if len(words) < 2:
        raise RaffleArgumentError(WINNER_USAGE)

    index = {normalize(name): name for name in item_names}
    readings: list[tuple[str, str]] = []
    saw_known_ign = False

    for i in range(1, len(words)):
        candidate_ign = " ".join(words[i:])
        try:
            player = items_rules.resolve_ign(candidate_ign, roster)
        except items_rules.RequestParseError as exc:
            raise RaffleArgumentError(str(exc)) from None
        if player is None:
            continue
        saw_known_ign = True
        item = index.get(normalize(" ".join(words[:i])))
        if item is not None:
            readings.append((item, player))

    if len(readings) == 1:
        return readings[0]
    if len(readings) > 1:
        spelled = "; ".join(f"{item!r} for {ign!r}" for item, ign in readings)
        raise RaffleArgumentError(
            f"That could be read more than one way ({spelled}). Refusing to guess."
        )
    if saw_known_ign:
        suggestions = get_close_matches(argument, item_names, n=3, cutoff=0.5)
        hint = f" Open raffles: {', '.join(suggestions)}." if suggestions else ""
        raise RaffleArgumentError(
            f"No open raffle matches that name.{hint} {WINNER_USAGE}"
        )

    tail = words[-1]
    suggestions = get_close_matches(tail, roster, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise RaffleArgumentError(
        f"No player named {tail!r} in the sheet.{hint} The IGN goes last: "
        f"{WINNER_USAGE}"
    )


# A hyphen only separates winners when it has whitespace on BOTH sides.
# 'wile-KAMOTE' is a roster row, so a bare hyphen cannot be the delimiter.
WINNER_SPLIT = re.compile(r"\s+-\s+")
DANGLING_SEPARATOR = re.compile(r"\s-\s*$")


def split_item_and_igns(
    argument: str, item_names: list[str], roster: list[str]
) -> tuple[str, list[str]]:
    """Split '<item> <IGN> - <IGN> - ...' into the item and its winners.

    The item may run into the first name or be followed by its own dash;
    officers type both. The two readings cannot collide, because the
    first is only taken when the leading chunk is EXACTLY a raffle name.

    Every name is resolved before this returns, so a typo is refused
    before any checkbox is ticked rather than half way through.
    """
    chunks = WINNER_SPLIT.split(argument.strip())
    # A separator needs whitespace on both sides, so only a hyphen with
    # whitespace BEFORE it is a dangling one. Testing the last character
    # alone would reject a roster name that simply ends in a hyphen.
    if DANGLING_SEPARATOR.search(argument) or any(
        not chunk.strip() for chunk in chunks
    ):
        raise RaffleArgumentError(
            f"There is an empty name between two dashes. {WINNER_USAGE}"
        )
    chunks = [chunk.strip() for chunk in chunks]

    index = {normalize(name): name for name in item_names}
    head = index.get(normalize(chunks[0]))
    if head is not None and len(chunks) == 1:
        raise RaffleArgumentError(f"Which player won **{head}**? {WINNER_USAGE}")

    if head is not None:
        item, igns = head, []
    else:
        item, first = split_item_and_ign(chunks[0], item_names, roster)
        igns = [first]

    for chunk in chunks[1:]:
        try:
            player = items_rules.resolve_ign(chunk, roster)
        except items_rules.RequestParseError as exc:
            raise RaffleArgumentError(str(exc)) from None
        if player is None:
            suggestions = get_close_matches(chunk, roster, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise RaffleArgumentError(
                f"No player named {chunk!r} in the sheet.{hint} {WINNER_USAGE}"
            )
        igns.append(player)

    # Aliases mean two different chunks can name one roster row, and a
    # repeat is always a miscount -- a player cannot win the same log
    # twice.
    counts = Counter(normalize(ign) for ign in igns)
    repeated = sorted({ign for ign in igns if counts[normalize(ign)] > 1})
    if repeated:
        raise RaffleArgumentError(
            f"{', '.join(repeated)} is named more than once. "
            "Each winner may only be listed once."
        )

    return item, igns
