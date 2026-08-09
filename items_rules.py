"""Allocation rules for the item distribution bot.

Pure logic only -- no Google Sheets, no Discord, no clock reads except
now_pht(). Everything here is decided from values the caller passes in,
which is what makes the caps testable without a network.

The guild's two rules:
  * a special log may be received once, ever
  * gear logs are capped at three per player per PHT day, any mix

The second rule cannot be answered from the Gear Logs tab: its cells hold
lifetime totals with no dates. It is answered from the Distribution Log
ledger instead, which is why gear_used_today takes ledger rows.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from attendance_roster import ALIASES, normalize

PHT = ZoneInfo("Asia/Manila")

# Ledger timestamps are PHT local time with no offset suffix. That makes
# "which day is this row on" a string prefix comparison rather than a
# parse-and-convert, so a malformed row can never be silently counted
# into the wrong day.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_GEAR_DAILY_CAP = 3

SPECIAL = "Special"
GEAR = "Gear"


def now_pht() -> datetime:
    return datetime.now(PHT)


def format_timestamp(moment: datetime) -> str:
    return moment.strftime(TIMESTAMP_FORMAT)


def pht_day(timestamp: str) -> str:
    """The date portion of a ledger timestamp, as 'YYYY-MM-DD'."""
    return timestamp.strip()[:10]


def gear_used_today(
    ledger_rows: list[list[str]],
    ign: str,
    today: str,
    *,
    timestamp_column: int = 0,
    ign_column: int = 1,
    type_column: int = 3,
) -> int:
    """How many gear logs this player has already been given today.

    Rows too short to hold the columns we need are skipped rather than
    raising: a half-written ledger row must not make the cap
    uncomputable and lock the player out entirely.
    """
    wanted = normalize(ign)
    count = 0
    for row in ledger_rows:
        if len(row) <= max(timestamp_column, ign_column, type_column):
            continue
        if row[type_column].strip() != GEAR:
            continue
        if normalize(row[ign_column]) != wanted:
            continue
        if pht_day(row[timestamp_column]) == today:
            count += 1
    return count


from dataclasses import dataclass
from difflib import get_close_matches

from attendance_bosses import header_base

PLAYER_COLUMN_HEADER = "Player Name"


class ItemLookupError(RuntimeError):
    """The item name does not resolve to exactly one column."""


@dataclass(frozen=True)
class ResolvedItem:
    name: str
    type: str


def item_names(headers: list[str]) -> list[str]:
    """Header cells that name an item.

    header_base strips a ' - N' annotation, matching the attendance
    sheet's header convention. Blanks (spacer columns) and the player
    column are not items.
    """
    names = []
    for cell in headers:
        base = header_base(cell)
        if not base or base == PLAYER_COLUMN_HEADER:
            continue
        names.append(base)
    return names


def _suggest(query: str, names: list[str]) -> list[str]:
    """Item names worth offering after a failed lookup.

    Substring hits come first and carry the weight: a member who types
    'Asta' wants to see every Asta item, but difflib scores 'Asta'
    against "Asta's Heart" at 0.5 -- below any cutoff loose enough to be
    safe -- so close-matching alone would offer nothing at all. Close
    matches still follow, to catch the transposed-letter typo that a
    substring search cannot.
    """
    wanted = normalize(query)
    hits = [name for name in names if wanted and wanted in normalize(name)]
    for name in get_close_matches(query, names, n=3, cutoff=0.6):
        if name not in hits:
            hits.append(name)
    return hits[:3]


def _exact(query: str, names: list[str]) -> str | None:
    wanted = normalize(query)
    for name in names:
        if normalize(name) == wanted:
            return name
    return None


RAFFLE_REDIRECT = (
    "Special logs are raffled, not requested: watch the raffle channel for "
    "a `!poll` and answer it there."
)
REQUEST_REDIRECT = "Gear logs are requested with `!request`, not raffled."


def resolve_item(
    query: str, special_headers: list[str], gear_headers: list[str]
) -> ResolvedItem:
    """The gear item this query names, refusing anything else.

    Requires an exact (case- and spacing-insensitive) header match. Fuzzy
    matching is deliberately NOT used here: item names differ by one word
    ("Asta's Belt" vs "Asta's Heart"), so a near match would hand out the
    wrong item -- and unlike attendance, an approval is a permanent
    record. Close names are offered as suggestions instead.

    A special log is refused rather than returned: those are raffled now.
    The special headers are still read, and that is the point -- knowing
    the name IS a special log is what turns a dead end into a redirect.
    Dropping them would leave the member with "no item column named
    that", which does not say where to go instead.
    """
    if not query.strip():
        raise ItemLookupError("No item name given.")

    specials = item_names(special_headers)
    gears = item_names(gear_headers)

    in_special = _exact(query, specials)
    in_gear = _exact(query, gears)

    if in_special and in_gear:
        raise ItemLookupError(
            f"{in_special!r} appears in both Special Logs and Gear Logs; "
            "refusing to guess which one is meant. Remove the duplicate column."
        )
    if in_special:
        raise ItemLookupError(f"{in_special!r} is a special log. {RAFFLE_REDIRECT}")
    if in_gear:
        return ResolvedItem(name=in_gear, type=GEAR)

    suggestions = _suggest(query, specials + gears)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ItemLookupError(f"No item column named {query!r}.{hint}")


def resolve_special(
    query: str, special_headers: list[str], gear_headers: list[str]
) -> str:
    """The Special Logs header this query names, exactly.

    The raffle's counterpart to resolve_item, and exact for the same
    reason: item names differ by one word, and the write this feeds
    ticks a checkbox that can never be untidied by the bot.
    """
    if not query.strip():
        raise ItemLookupError("No item name given.")

    specials = item_names(special_headers)
    gears = item_names(gear_headers)

    found = _exact(query, specials)
    if found:
        return found

    in_gear = _exact(query, gears)
    if in_gear:
        raise ItemLookupError(f"{in_gear!r} is a gear log. {REQUEST_REDIRECT}")

    suggestions = _suggest(query, specials)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ItemLookupError(f"No special log column named {query!r}.{hint}")


class RequestParseError(RuntimeError):
    """The !request argument does not resolve to one item and one player."""


@dataclass(frozen=True)
class ParsedRequest:
    item: ResolvedItem
    ign: str


def resolve_ign(query: str, roster: list[str]) -> str | None:
    """The roster entry this name refers to, or None.

    Exact (normalized) match, plus attendance_roster.ALIASES so a member
    typing their decorated in-game name ('ツRyuuツ') reaches their sheet
    row ('Ryuu'). Some in-game names share no characters at all with
    their sheet row, so aliases are the only possible resolution path
    for those players -- no fuzzy threshold could ever match them.

    Fuzzy matching is deliberately not used: a wrong match here would
    credit an item to someone else, permanently.
    """
    index: dict[str, str] = {}
    for player in roster:
        if not player.strip():
            continue
        key = normalize(player)
        if key in index and index[key] != player:
            raise RequestParseError(
                f"{index[key]!r} and {player!r} both normalise to the same "
                f"name ({key!r}); refusing to guess"
            )
        index[key] = player
    wanted = normalize(query)
    if wanted in index:
        return index[wanted]
    for alias, target in ALIASES.items():
        if normalize(alias) == wanted and normalize(target) in index:
            return index[normalize(target)]
    return None


def parse_request(
    argument: str,
    roster: list[str],
    special_headers: list[str],
    gear_headers: list[str],
) -> ParsedRequest:
    """Split '<item name> <IGN>' where BOTH parts may contain spaces.

    Position-based splitting cannot work: the roster contains
    'chinchong ni Mumu'. Every split point is tried instead, and a split
    is accepted only when its suffix is a known player AND its prefix is
    a known item. Longest IGN first, so 'chinchong ni Mumu' wins over a
    hypothetical player called 'Mumu'.
    """
    words = argument.split()
    if len(words) < 2:
        raise RequestParseError(
            "Usage: `!request <item name> <IGN>` -- for example "
            "`!request Asta's Heart Kobe`"
        )

    matches: list[ParsedRequest] = []
    item_errors: list[str] = []
    saw_known_ign = False

    # split at index i => words[:i] is the item, words[i:] is the IGN.
    # Descending i would try the SHORTEST ign first; ascending tries the
    # longest, which is what disambiguates multi-word names.
    for i in range(1, len(words)):
        candidate_ign = " ".join(words[i:])
        player = resolve_ign(candidate_ign, roster)
        if player is None:
            continue
        saw_known_ign = True
        try:
            item = resolve_item(
                " ".join(words[:i]), special_headers, gear_headers
            )
        except ItemLookupError as exc:
            item_errors.append(str(exc))
            continue
        matches.append(ParsedRequest(item=item, ign=player))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        readings = "; ".join(f"{m.item.name!r} for {m.ign!r}" for m in matches)
        raise RequestParseError(
            f"That could be read more than one way ({readings}). "
            "Refusing to guess."
        )
    if saw_known_ign:
        raise RequestParseError(item_errors[0])

    tail = words[-1]
    suggestions = get_close_matches(tail, roster, n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise RequestParseError(
        f"No player named {tail!r} in the sheet.{hint} "
        "The IGN goes last: `!request <item name> <IGN>`"
    )


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reason: str
    used: int = 0
    cap: int = 0


def check_eligibility(
    item_type: str,
    ign: str,
    ledger_rows: list[list[str]],
    today: str,
    *,
    already_has_special: bool,
    pending_gear: int = 0,
    cap: int = DEFAULT_GEAR_DAILY_CAP,
) -> Eligibility:
    """Whether this player may receive this item right now.

    Called twice per approval: once at !request for fast feedback, and
    again inside the write lock when an officer clicks approve. The
    second call is not redundant -- without it, a member queues several
    requests before any is approved and every one of them passes the
    first check.

    pending_gear is why the first call is meaningful at all: queued but
    unapproved gear requests count, so a member cannot stack four
    requests and have officers approve them one at a time, each
    individually looking within the cap.
    """
    if item_type == SPECIAL:
        if already_has_special:
            return Eligibility(
                allowed=False,
                reason="already has this special log -- it can only be received once",
            )
        return Eligibility(allowed=True, reason="eligible")

    used = gear_used_today(ledger_rows, ign, today) + pending_gear
    if used >= cap:
        return Eligibility(
            allowed=False,
            reason=f"already at the daily gear limit ({used}/{cap}) -- resets at midnight PHT",
            used=used,
            cap=cap,
        )
    return Eligibility(allowed=True, reason="eligible", used=used, cap=cap)
