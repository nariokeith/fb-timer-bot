"""Point values, and resolving a typed boss name against the sheet's headers.

The sheet's header row is the source of truth for which bosses have an
attendance column, so there is no boss table to keep in sync with the
timer -- and this module deliberately does not import bot.py. Adding a
boss column to the sheet makes it loggable with no code change.
"""

# Confirmed by the guild owner on 2026-08-06. Everything else is worth 1.
BOSSES_WORTH_3 = frozenset({
    "Lucus", "Libitina", "Rakajeth", "Icaruthia",
    "Motti", "Nevaeh", "Tumier", "Camalia",
})

# Columns that exist in the sheet but are not bosses.
NON_BOSS_HEADERS = frozenset({"player name", "points"})

_POINTS_INDEX = {name.casefold(): 3 for name in BOSSES_WORTH_3}


class BossNotFound(ValueError):
    """No column in the sheet matches this name."""


class BossAmbiguous(ValueError):
    """More than one column matches this name."""


def header_base(header: str) -> str:
    """The boss name inside a header cell.

    Some headers annotate their point value, e.g. "Lucus - 3".
    """
    return header.split(" - ")[0].strip()


def boss_points(boss: str) -> int:
    """Points awarded for one attendance at this boss."""
    return _POINTS_INDEX.get(boss.strip().casefold(), 1)


def _boss_headers(headers: list[str]) -> list[str]:
    names = []
    for cell in headers:
        base = header_base(cell)
        if base and base.casefold() not in NON_BOSS_HEADERS:
            names.append(base)
    return names


def resolve_boss(headers: list[str], query: str) -> str:
    """Match a typed name against the sheet's boss columns.

    Case-insensitive exact match first, then unique prefix -- the same
    convention bot.py uses for its own commands. Never guesses: an input
    matching nothing, or several columns, raises.
    """
    wanted = query.strip().casefold()
    if not wanted:
        raise BossNotFound("No boss name given")

    names = _boss_headers(headers)

    for name in names:
        if name.casefold() == wanted:
            return name

    matches = [name for name in names if name.casefold().startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise BossAmbiguous(
            f"{query!r} matches several columns: {', '.join(sorted(matches))}"
        )
    raise BossNotFound(f"No column matches {query!r}")
