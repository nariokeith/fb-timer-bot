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
from collections.abc import Callable, Sequence
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
class Identities:
    """The three places a Discord account can be tied to a roster row.

    Passed in rather than read here so this module stays pure: the bot
    binds it from the state it already holds.
    """

    bindings: dict[str, str]
    not_players: frozenset[str]
    request_igns: dict[str, str]


@dataclass(frozen=True)
class VoterSplit:
    eligible: list[str] = field(default_factory=list)
    already_have: list[str] = field(default_factory=list)
    # Blocks the freeze: a voter nobody can name must not be silently
    # dropped from a pool a winner is drawn from.
    unidentified: list[Voter] = field(default_factory=list)
    # Resolved only through the IGN they last requested under, which may
    # be an alt -- shown separately so an officer can check before drawing.
    from_request: list[tuple[Voter, str]] = field(default_factory=list)
    skipped: list[Voter] = field(default_factory=list)
    # Collapsed as a repeat of a player already counted. The pool is right
    # either way, but an officer seeing two accounts on one row is how a
    # wrong binding gets noticed.
    duplicates: list[tuple[Voter, str]] = field(default_factory=list)


def _in_roster(ign: str, roster: list[str]) -> str | None:
    """The roster spelling of this IGN, or None when it is not one."""
    try:
        return items_rules.resolve_ign(ign, roster)
    except items_rules.RequestParseError:
        # Two roster rows normalise identically. That is a sheet problem,
        # and nobody can be resolved safely against it.
        return None


def resolve_identity(
    voter: Voter, roster: list[str], identities: Identities
) -> tuple[str | None, str | None]:
    """The roster row this voter is, and which rung of the ladder said so.

    A binding is tried before the nickname so an officer can correct a
    nickname that resolves to the wrong player. A binding or fallback
    naming a row that has since left the sheet resolves to nothing rather
    than staying drawable.
    """
    key = str(voter.user_id)

    bound = identities.bindings.get(key)
    if bound is not None:
        player = _in_roster(bound, roster)
        return (player, "binding") if player is not None else (None, None)

    if key in identities.not_players:
        return None, "skipped"

    player = resolve_voter(voter.display_name, roster)
    if player is not None:
        return player, "nickname"

    requested = identities.request_igns.get(key)
    if requested is not None:
        player = _in_roster(requested, roster)
        return (player, "request") if player is not None else (None, None)

    return None, None


def classify_voters(
    voters: list[Voter],
    roster: list[str],
    holds: Callable[[str], bool],
    identities: Identities,
) -> VoterSplit:
    """Split the poll's voters into the groups an officer needs.

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
    from_request: list[tuple[Voter, str]] = []
    skipped: list[Voter] = []
    duplicates: list[tuple[Voter, str]] = []
    seen: set[str] = set()

    for voter in voters:
        player, source = resolve_identity(voter, roster, identities)
        if source == "skipped":
            skipped.append(voter)
            continue
        if player is None:
            unidentified.append(voter)
            continue
        key = normalize(player)
        if key in seen:
            duplicates.append((voter, player))
            continue
        seen.add(key)
        if source == "request":
            from_request.append((voter, player))
        (already_have if holds(player) else eligible).append(player)

    return VoterSplit(
        eligible=eligible,
        already_have=already_have,
        unidentified=unidentified,
        from_request=from_request,
        skipped=skipped,
        duplicates=duplicates,
    )


def remaining_pool(
    eligible: Sequence[str], won: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Split a frozen pool into who may still be drawn and who may not.

    The second list is everyone already holding a win from this session.
    They are returned rather than dropped so the officer can be shown why
    the pool shrank: a pool that quietly gets smaller is how a wrong
    exclusion goes unnoticed.

    Compared through normalize, not as raw strings, because an alias left
    by a roster rename would otherwise let a session winner back in.
    """
    taken = {normalize(name) for name in won}
    pool = [name for name in eligible if normalize(name) not in taken]
    excluded = [name for name in eligible if normalize(name) in taken]
    return pool, excluded


DEFAULT_POLL_HOURS = 24
MIN_POLL_HOURS = 1
# Discord allows longer, but a week is already far past any real raffle
# and a bounded value can never be rejected by the API mid-command.
MAX_POLL_HOURS = 168

HOURS_FLAG = "--hours"

POLL_USAGE = "Usage: `!poll <special log name> [--hours N]`"


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


# A hyphen only separates winners when it has whitespace on BOTH sides.
# 'wile-KAMOTE' is a roster row, so a bare hyphen cannot be the delimiter.
WINNER_SPLIT = re.compile(r"\s+-\s+")
DANGLING_SEPARATOR = re.compile(r"\s-\s*$")


WON_USAGE = (
    "Usage: `!won <IGN>`, or `!won <IGN> - <IGN> - <IGN>` for several "
    "winners of the same log."
)


def split_igns(argument: str, roster: list[str]) -> list[str]:
    """The winners named in a `!won` argument, in roster spelling.

    The session supplies the log, so this parses names only -- none of
    the item/IGN boundary guessing the old item-plus-name command needed.

    Every name is resolved before this returns, so a typo in the third
    name is refused before any checkbox is ticked rather than half way
    through.
    """
    text = argument.strip()
    if not text:
        raise RaffleArgumentError(f"Which player won? {WON_USAGE}")

    chunks = WINNER_SPLIT.split(text)
    # A separator needs whitespace on both sides, so only a hyphen with
    # whitespace BEFORE it is a dangling one. Testing the last character
    # alone would reject a roster name that simply ends in a hyphen.
    if DANGLING_SEPARATOR.search(argument) or any(
        not chunk.strip() for chunk in chunks
    ):
        raise RaffleArgumentError(
            f"There is an empty name between two dashes. {WON_USAGE}"
        )

    igns: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        try:
            player = items_rules.resolve_ign(chunk, roster)
        except items_rules.RequestParseError as exc:
            raise RaffleArgumentError(str(exc)) from None
        if player is None:
            suggestions = get_close_matches(chunk, roster, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise RaffleArgumentError(
                f"No player named {chunk!r} in the sheet.{hint} {WON_USAGE}"
            )
        igns.append(player)

    # Aliases mean two different chunks can name one roster row, and a
    # repeat is always a miscount -- a player cannot win one log twice.
    counts = Counter(normalize(ign) for ign in igns)
    repeated = sorted({ign for ign in igns if counts[normalize(ign)] > 1})
    if repeated:
        raise RaffleArgumentError(
            f"{', '.join(repeated)} is named more than once. "
            "Each winner may only be listed once."
        )
    return igns
