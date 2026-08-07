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
