"""Attendance logging bot for the Lordnine guild.

A second Discord application, separate from the field boss timer. It does
not import bot.py, share its token, or run in its process -- the timer is
in production and must not be affected by anything here.

Started by supervisor.py. Exits with EXIT_NOT_CONFIGURED when its
credentials are absent, which the supervisor treats as a deliberate stop,
so the timer runs normally on a deploy that predates these secrets.
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from dotenv import load_dotenv

from attendance_bosses import BossAmbiguous, BossNotFound, boss_points, resolve_boss
from attendance_roster import DuplicatePlayerName, match_names
from attendance_sheet import (
    SheetStructureError,
    append_log_entry,
    apply_writes,
    find_column,
    image_already_logged,
    last_unreversed_entry,
    mark_entry_reversed,
    open_spreadsheet,
    plan_point_writes,
    read_config,
    read_headers,
    read_players,
    write_config,
)
from attendance_vision import VisionError, extract_names

load_dotenv()

EXIT_NOT_CONFIGURED = 78  # must match supervisor.NO_RESTART_CODES

TOKEN = os.getenv("ATTENDANCE_DISCORD_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

CONFIRM_EMOJI = "✅"
PREVIEW_TIMEOUT = 180  # seconds
EMBED_COLOR = discord.Color.orange()  # matches the timer's look

# The guild reads Manila time; Render runs UTC. A naive datetime.now()
# would stamp every log row eight hours off, and that value is shown back
# to officers in the undo footer.
TIMEZONE = ZoneInfo("Asia/Manila")

# Serialises every sheet mutation this process performs.
#
# attendance_sheet's plan_point_writes/apply_writes pair is a
# read-modify-write across two round trips: plan reads the current cell
# values and apply writes absolute values computed from that read. Two
# officers confirming at the same moment would both read 1 and both write
# 2, so one attendance silently vanishes with no error anywhere. The same
# hazard applies to last_unreversed_entry -> mark_entry_reversed, and to
# write_config, which reads the config tab to find the row to replace.
#
# Both of those modules deliberately do not lock; their docstrings name
# the command layer -- this file -- as the place that must. The lock is
# held across each whole critical section (see _locked), never while
# waiting on an officer's reaction: that would block every other command
# for up to PREVIEW_TIMEOUT seconds.
_SHEET_LOCK = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True

# help_command=None because the field boss timer defines its own !help in
# the same guild with the same prefix. Leaving discord.py's built-in one
# enabled makes both applications answer !help, and makes this bot reply
# "No command called 'killed' found" to the timer's !help killed. That is
# a visible regression in production behaviour. !attendancehelp covers
# this bot's own commands, and on_command_error swallows the resulting
# CommandNotFound.
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# Discord refuses an embed whose description exceeds this, and the two
# partial-write messages below are exactly the ones that must never fail
# to render: if working.edit raises, the officer sees a generic error
# instead of "do not re-run", re-runs, and the points land twice -- the
# outcome those exceptions exist to prevent. The variable parts are
# therefore clamped individually, so the fixed instructional text (which
# carries the whole meaning) survives intact under any input. Budget:
# 800 + 2200 + 100 + 100 = 3200, against roughly 600 characters of fixed
# text, leaving comfortable headroom under 4096.
EMBED_DESCRIPTION_LIMIT = 4096
CAUSE_LIMIT = 800
PLAYERS_LIMIT = 2200
NAME_LIMIT = 100


def _clip(text: str, limit: int) -> str:
    """`text` shortened to `limit` characters, marked when shortened."""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _clip_players(players: list[str], limit: int = PLAYERS_LIMIT) -> str:
    """The player list, saying plainly when it is only part of it.

    A silently truncated list is worse than a short one: the officer would
    take the visible names for everyone affected and reconcile only those.
    """
    names = [str(p).strip() for p in players if str(p).strip()]
    joined = ", ".join(names)
    if len(joined) <= limit:
        return joined

    kept: list[str] = []
    for index, name in enumerate(names):
        candidate = ", ".join([*kept, name])
        suffix = f", … and {len(names) - index - 1} more"
        if len(candidate) + len(suffix) > limit:
            break
        kept.append(name)

    remaining = len(names) - len(kept)
    if not kept:
        return f"… and {remaining} more"
    return ", ".join(kept) + f", … and {remaining} more"


class PointsWrittenButNotLogged(RuntimeError):
    """apply_writes succeeded; append_log_entry did not.

    A two-stage mutation can fail between the stages. Reporting this as a
    plain failure ("Nothing was written") is actively dangerous: the
    points ARE in the sheet, so the officer re-runs the command and pays
    twice -- and because the audit row is missing, a later
    !undoattendance would reverse some earlier entry instead.
    """

    def __init__(self, *, tab, boss, points, players, cause_text):
        self.tab = tab
        self.boss = boss
        self.points = points
        self.players = list(players)
        self.cause_text = cause_text
        super().__init__(
            f"points for {boss} were added to {tab} but the log row was not "
            f"written: {cause_text}"
        )

    @property
    def description(self) -> str:
        return (
            f"**+{self.points}** for **{_clip(self.boss, NAME_LIMIT)}** "
            f"**were added** to **{_clip(self.tab, NAME_LIMIT)}** for "
            f"{len(self.players)} player"
            f"{'s' if len(self.players) != 1 else ''}, but the audit log row "
            f"could not be written.\n\n"
            f"Please do **not re-run** this command — the points are already "
            f"in the sheet and running it again would add them twice. "
            f"`!undoattendance` cannot reverse this either, because there is "
            f"no log row for it; it must be reconciled by hand.\n\n"
            f"Players: {_clip_players(self.players)}\n\n"
            f"Reason: {_clip(self.cause_text, CAUSE_LIMIT)}"
        )


class PointsRemovedButNotMarked(RuntimeError):
    """The undo's subtraction succeeded; mark_entry_reversed did not.

    The mirror of PointsWrittenButNotLogged. The log row is still
    reversed="", so mark_entry_reversed's attachment-id guard cannot catch
    a retry -- the row genuinely is unreversed -- and a second
    !undoattendance would subtract the same points again.
    """

    def __init__(self, *, entry, cause_text):
        self.entry = dict(entry)
        self.cause_text = cause_text
        super().__init__(
            f"points for {entry['boss']} were removed but the log entry could "
            f"not be marked reversed: {cause_text}"
        )

    @property
    def description(self) -> str:
        try:
            players = _parse_players(str(self.entry.get("players", "[]")))
        except SheetStructureError:
            # Display only: even if the players cell were somehow
            # unparseable, the do-not-re-run warning must still render.
            players = [str(self.entry.get("players", ""))]
        return (
            f"**{self.entry['points_each']}** points for "
            f"**{_clip(self.entry['boss'], NAME_LIMIT)}** have **already** "
            f"been removed from **{_clip(self.entry['tab'], NAME_LIMIT)}**, "
            f"but the log entry could not be marked reversed.\n\n"
            f"Please do **not re-run** `!undoattendance` — the entry still "
            f"looks live, so running it again would remove those points "
            f"twice. Mark the row reversed by hand instead.\n\n"
            f"Players: {_clip_players(players)}\n\n"
            f"Reason: {_clip(self.cause_text, CAUSE_LIMIT)}"
        )


class AlreadyLoggedDuringPreview(RuntimeError):
    """Another officer confirmed this same screenshot during the 180s wait."""


def _timeout_description(
    *,
    verb: str,
    outcome: str,
    tab: str | None,
    boss: str | None,
    points: int | None,
    players: list[str] | None,
) -> str:
    """Wording for a LOCK_TIMEOUT that fires mid-commit or mid-undo.

    Unlike PointsWrittenButNotLogged/PointsRemovedButNotMarked, this is
    NOT a confirmed partial state: asyncio.timeout only stops the
    AWAITING coroutine from waiting (see _locked) -- it cannot stop the
    to_thread worker actually talking to Sheets. That worker may already
    have finished, may be partway through, or may not have started the
    write/removal yet by the time this fires. So, unlike the other two
    partial-write messages, this one must not assert either outcome --
    only that it is unknown, and that re-running before checking risks
    paying (or removing) the same points twice.
    """
    boss_text = f"**{_clip(boss, NAME_LIMIT)}**" if boss else "the boss just requested"
    tab_text = f" in **{_clip(tab, NAME_LIMIT)}**" if tab else ""
    points_text = (
        f" (**{points}** point{'s' if points != 1 else ''} each)"
        if points is not None
        else ""
    )
    players_text = f"\n\nPlayers: {_clip_players(players)}" if players else ""
    return (
        f"The sheet did not respond in time while {verb} for {boss_text}"
        f"{tab_text}{points_text}.\n\n"
        f"**It is not known whether the {outcome} actually happened.** "
        f"The request may still be finishing in the background even though "
        f"this command gave up waiting for it.\n\n"
        f"**Check the sheet before re-running.** If it already landed, "
        f"re-running now would {outcome} the same points a second time."
        f"{players_text}"
    )


def _timestamp() -> str:
    """Now, in the guild's timezone, ISO with second resolution."""
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def make_embed(
    title: str, description: str | None = None, footer: str | None = None
) -> discord.Embed:
    """Same shape as the timer's embeds, implemented locally.

    Deliberately duplicated rather than imported: importing bot.py would
    reintroduce the coupling this whole design removes.
    """
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    if footer:
        embed.set_footer(text=footer)
    return embed


def error_text(exc: BaseException) -> str:
    """A never-empty human description of an exception.

    gspread re-raises a permissions APIError as a bare PermissionError,
    and str(PermissionError()) is the empty string -- rendering that into
    an embed produces a completely blank body telling the officer nothing
    at all. The real diagnostic in that case sits on __cause__. Every
    error embed in this module routes its text through here.
    """
    cause = exc.__cause__
    candidates = (str(exc), str(cause) if cause is not None else "", repr(exc))
    for candidate in candidates:
        text = candidate.strip()
        if text:
            return text
    return repr(exc)  # unreachable in practice; repr is never empty


# ---------------------------------------------------------------------------
# Blocking helpers. Every one runs through asyncio.to_thread: gspread and
# google-genai are synchronous, and blocking this bot's event loop would
# make it stop answering commands.
# ---------------------------------------------------------------------------


def _spreadsheet():
    return open_spreadsheet(SHEET_ID, SERVICE_ACCOUNT_JSON)


def _officer_role_id(spreadsheet) -> int | None:
    raw = read_config(spreadsheet).get("officer_role_id", "")
    return int(raw) if raw.isdigit() else None


def _is_officer(member, role_id: int | None) -> bool:
    if role_id is None:
        return False
    return any(role.id == role_id for role in getattr(member, "roles", []))


def _parse_players(raw: str) -> list[str]:
    """The player list from a log entry's `players` cell.

    Stored as JSON (see attendance_cmd) rather than comma-joined-and-split:
    a player name that itself contains a comma would otherwise round-trip
    into fragments, and !undoattendance would then either refuse forever
    (wrong player count vs. the sheet) or silently subtract points for
    fragments instead of the real name.
    """
    try:
        players = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise SheetStructureError(
            f"_BotLog row 'players' cell is not valid JSON: {raw!r}"
        ) from None
    if not isinstance(players, list) or not all(isinstance(p, str) for p in players):
        raise SheetStructureError(
            f"_BotLog row 'players' cell is not a JSON list of strings: {raw!r}"
        )
    return players


def _load_context(spreadsheet, boss_query: str) -> dict:
    """Everything the preview needs except the duplicate flag.

    The duplicate flag needs the image's hash, which is only known once
    the attachment has been downloaded -- see image_already_logged, called
    separately by the caller once it has the bytes.

    Read-only, so it does not need _SHEET_LOCK: nothing it returns is
    written back without a fresh read inside the locked commit. Takes an
    already-authorised `spreadsheet` (the caller fetches one per command
    and reuses it here and for the officer check, instead of each helper
    re-authorising its own) and reads the target tab's grid exactly once
    for the header lookup, the boss-column check, and the player list --
    instead of three separate get_all_values() calls against the same
    sheet.
    """
    config = read_config(spreadsheet)

    tab = config.get("target_tab")
    if not tab:
        raise SheetStructureError("No target tab set. Run !setweek <tab name> first.")

    worksheet = spreadsheet.worksheet(tab)
    grid = worksheet.get_all_values()
    boss = resolve_boss(read_headers(worksheet, grid), boss_query)

    # find_column's result is deliberately discarded: calling it here only
    # fails fast, before a Gemini request, if the boss has no column or
    # has two. The index itself must not be carried across the preview --
    # see _commit.
    find_column(worksheet, boss, grid)

    return {
        "tab": tab,
        "boss": boss,
        "points": boss_points(boss),
        "players": read_players(worksheet, grid),
    }


def _commit(
    tab: str,
    boss: str,
    players: list[str],
    points: int,
    entry: dict,
    was_duplicate: bool,
) -> None:
    """Add the points and record the log entry. Run only via _locked.

    Takes the boss NAME, not a column index. attendance_sheet locates
    cells by content precisely so that reordering columns breaks nothing;
    caching an index across the up-to-180-second preview would put that
    coupling back, and an inserted column would silently send the write
    into some other boss's column. Re-resolving here either lands on the
    column headed by the boss the preview promised, or find_column
    refuses loudly.

    `was_duplicate` is what the preview told the officer. If the
    screenshot was NOT flagged then but is logged now, another officer
    confirmed it during the wait and this would double-pay, so it aborts.
    If it WAS flagged, the officer saw the warning and chose to proceed --
    a legitimate re-log, e.g. after an undo.

    Always fetches its own spreadsheet handle and its own fresh grid, run
    inside the lock -- never a handle or grid cached from before the
    preview. The grid is still read only ONCE here (reused for both
    find_column and plan_point_writes) rather than twice, but that read
    itself happens right now, under the lock, same as before.
    """
    spreadsheet = _spreadsheet()

    if not was_duplicate and image_already_logged(spreadsheet, entry["image_sha256"]):
        raise AlreadyLoggedDuringPreview(
            "This screenshot was logged by someone else while this preview "
            "was open. Nothing was written, to avoid paying twice."
        )

    worksheet = spreadsheet.worksheet(tab)
    grid = worksheet.get_all_values()
    column = find_column(worksheet, boss, grid)
    apply_writes(
        worksheet, plan_point_writes(worksheet, players, column, points, grid)
    )

    # Past this line the points are in the sheet. Anything that fails now
    # must be reported as a partial write, never as "nothing was written".
    try:
        append_log_entry(spreadsheet, entry)
    except Exception as exc:
        raise PointsWrittenButNotLogged(
            tab=tab,
            boss=boss,
            points=points,
            players=players,
            cause_text=error_text(exc),
        ) from exc


def _reverse_last() -> dict | None:
    """Subtract the last live entry's points and flag it. Run only via _locked."""
    spreadsheet = _spreadsheet()
    found = last_unreversed_entry(spreadsheet)
    if found is None:
        return None

    row_number, entry = found
    players = _parse_players(entry["players"])
    worksheet = spreadsheet.worksheet(entry["tab"])
    grid = worksheet.get_all_values()
    # Resolved by name inside the lock, same reasoning as _commit.
    column = find_column(worksheet, entry["boss"], grid)

    apply_writes(
        worksheet,
        plan_point_writes(
            worksheet, players, column, -int(entry["points_each"]), grid
        ),
    )

    # Past this line the points are gone from the sheet.
    try:
        # The third argument is the row's attachment_id as just read: it
        # makes mark_entry_reversed refuse if the row changed underneath
        # us. It cannot catch a retry of THIS failure, though -- the row
        # really is still unreversed -- hence the distinct exception.
        mark_entry_reversed(spreadsheet, row_number, entry["attachment_id"])
    except Exception as exc:
        raise PointsRemovedButNotMarked(
            entry=entry, cause_text=error_text(exc)
        ) from exc
    return entry


def _set_target_tab(tab: str) -> None:
    """Point future attendance at `tab`. Run only via _locked."""
    spreadsheet = _spreadsheet()
    spreadsheet.worksheet(tab)  # raises if it does not exist
    write_config(spreadsheet, "target_tab", tab)


def _set_officer_role(role_id: str) -> None:
    """Store the officer role id. Run only via _locked."""
    write_config(_spreadsheet(), "officer_role_id", role_id)


# A stuck gspread call has no timeout of its own by default (see
# attendance_sheet.REQUEST_TIMEOUT, which now bounds each individual
# request) but several such requests can still chain together inside one
# _locked call. This is the backstop: if the whole locked section is still
# running after LOCK_TIMEOUT, it is abandoned so _SHEET_LOCK is released
# and the officer sees a clear refusal -- instead of every later command
# hanging silently on "Working on it..." forever because the lock never
# came free. 45s is comfortably above REQUEST_TIMEOUT (15s) so a single
# slow-but-not-hung request has room to finish normally, while still being
# short enough that the officer isn't left waiting indefinitely.
LOCK_TIMEOUT = 45.0


async def _locked(func, *args):
    """Run one blocking sheet mutation as a single serialised unit.

    The lock covers the whole read-plan-write section, not the individual
    gspread calls, and is released the moment the work is done -- or the
    moment LOCK_TIMEOUT elapses, whichever comes first.
    """
    async with _SHEET_LOCK:
        # NOTE for the next reader: when LOCK_TIMEOUT fires here, only the
        # AWAITING coroutine is interrupted -- `_SHEET_LOCK` is released in
        # this `async with`'s __aexit__ as soon as TimeoutError propagates,
        # but the asyncio.to_thread WORKER underneath keeps running in its
        # OS thread. CPython has no way to forcibly stop a thread executing
        # synchronous code, so `func` (e.g. _commit) may still be mid-write
        # -- or may finish writing -- after this function has already
        # raised and the lock is free again for the next command. What
        # actually bounds how long that orphan can keep talking to Sheets
        # is REQUEST_TIMEOUT on the gspread client (attendance_sheet.py),
        # NOT this asyncio.timeout. Callers must not report a LOCK_TIMEOUT
        # as "nothing happened" -- see _timeout_description and its call
        # sites in attendance_cmd / undo_attendance_cmd.
        async with asyncio.timeout(LOCK_TIMEOUT):
            return await asyncio.to_thread(func, *args)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def _reject(ctx, title: str, description: str, footer: str | None = None):
    await ctx.send(embed=make_embed(title, description, footer=footer))


async def _safe_reject(ctx, title: str, description: str):
    """_reject that cannot itself raise, for use inside the error handler."""
    try:
        await _reject(ctx, title, description)
    except Exception as exc:  # e.g. no permission to post in this channel
        print(f"Could not report {title!r}: {exc!r}", file=sys.stderr, flush=True)


async def _report_partial_failure(
    ctx, working, title: str, description: str, footer: str | None = None
):
    """Render a partial-write warning through whatever channel still works.

    PointsWrittenButNotLogged and PointsRemovedButNotMarked exist to carry
    one instruction an officer must see: don't re-run this, it already
    landed once and running it again pays (or subtracts) twice. Losing
    that instruction to a failed Discord call -- a rate limit, `working`
    having been deleted, the bot losing permission to edit or post in the
    channel -- would silently produce exactly the outcome the exception
    exists to prevent. So this never depends on a single call succeeding:

    1. stderr, unconditionally, first. The one channel that cannot itself
       fail to record the full detail.
    2. `working.edit(...)`, if a preview message was given.
    3. `ctx.send(embed=...)`, a fresh message in the same channel.
    4. `ctx.send(...)` with plain text and no embed, in case the failure
       is embed-specific rather than channel-specific.

    Each step is attempted only if the one before it raised; the first
    success ends it.
    """
    print(f"PARTIAL WRITE -- {title}: {description}", file=sys.stderr, flush=True)

    embed = make_embed(title, description, footer=footer)

    if working is not None:
        try:
            await working.edit(embed=embed)
            return
        except Exception as exc:
            print(
                f"working.edit failed while reporting {title!r}: {exc!r}",
                file=sys.stderr,
                flush=True,
            )

    try:
        await ctx.send(embed=embed)
        return
    except Exception as exc:
        print(
            f"ctx.send(embed=...) failed while reporting {title!r}: {exc!r}",
            file=sys.stderr,
            flush=True,
        )

    try:
        plain = title + "\n\n" + description + (f"\n\n{footer}" if footer else "")
        await ctx.send(plain)
    except Exception as exc:
        print(
            f"ctx.send(plain text) also failed while reporting {title!r}: {exc!r}",
            file=sys.stderr,
            flush=True,
        )


async def _open_spreadsheet_or_reject(ctx):
    """Authorise once for this command, or report and return None.

    Every command's first Sheets call. The caller reuses the returned
    handle for whatever else it needs (the officer check, _load_context,
    the duplicate check) instead of each of those re-authorising its own
    -- open_spreadsheet does a real API round trip (open_by_key), and
    doing it three or four times over for one Discord command was most of
    the read amplification against the 60-calls/minute quota.
    """
    try:
        return await asyncio.to_thread(_spreadsheet)
    except Exception as exc:
        await _reject(ctx, "❌ Sheet Unreachable", error_text(exc))
        return None


async def _require_officer(ctx, spreadsheet) -> int | None:
    """The configured officer role id, or None once a refusal has been sent."""
    try:
        role_id = await asyncio.to_thread(_officer_role_id, spreadsheet)
    except Exception as exc:
        await _reject(ctx, "❌ Sheet Unreachable", error_text(exc))
        return None

    if role_id is None:
        await _reject(
            ctx,
            "⚙️ Not Configured Yet",
            "No officer role is set.",
            footer="An admin must run !setofficerrole @role",
        )
        return None

    if not _is_officer(ctx.author, role_id):
        await _reject(
            ctx, "\U0001f6ab Officers Only", "Only officers can record attendance."
        )
        return None
    return role_id


@bot.event
async def on_ready():
    print(f"Attendance bot logged in as {bot.user} (ID: {bot.user.id}).", flush=True)


@bot.event
async def on_command_error(ctx, error):
    """Turn command-framework errors into something an officer can read.

    CommandNotFound is swallowed on purpose: this bot shares the "!"
    prefix with the field boss timer, which is a different application in
    the same guild. Without this, every timer command would print a
    CommandNotFound traceback into the supervisor's logs.
    """
    if isinstance(error, commands.CommandNotFound):
        return

    # stderr first, and unconditionally: it is the one report that cannot
    # itself fail. _safe_reject's send can (no permission to post in this
    # channel), and an exception escaping the error handler is silent.
    print(f"Command {ctx.command} failed: {error!r}", file=sys.stderr, flush=True)

    if isinstance(error, commands.MissingPermissions):
        await _safe_reject(
            ctx, "\U0001f6ab Admins Only", "That command needs Administrator."
        )
        return
    if isinstance(error, (commands.UserInputError, commands.CheckFailure)):
        await _safe_reject(ctx, "❓ Couldn't Run That", error_text(error))
        return

    await _safe_reject(ctx, "❌ Something Went Wrong", error_text(error))


@bot.command(name="attendance")
async def attendance_cmd(ctx: commands.Context, *, boss_name: str = ""):
    """Log attendance from a roster screenshot: !attendance <boss> + image"""
    spreadsheet = await _open_spreadsheet_or_reject(ctx)
    if spreadsheet is None:
        return

    role_id = await _require_officer(ctx, spreadsheet)
    if role_id is None:
        return

    if not boss_name:
        await _reject(
            ctx,
            "❓ Which Boss?",
            "Usage: `!attendance <boss>` with a roster screenshot attached.",
            footer="e.g. !attendance Lucus",
        )
        return

    if not ctx.message.attachments:
        await _reject(
            ctx,
            "\U0001f5bc️ No Screenshot",
            "Attach a party or guild roster screenshot to the same message.",
        )
        return

    attachment = ctx.message.attachments[0]
    content_type = attachment.content_type or ""
    if not content_type.startswith("image/"):
        await _reject(
            ctx, "\U0001f5bc️ Not An Image", "That attachment is not an image."
        )
        return

    working = await ctx.send(
        embed=make_embed("\U0001f50e Reading Screenshot", "Working on it...")
    )

    try:
        context = await asyncio.to_thread(_load_context, spreadsheet, boss_name)
    except (BossNotFound, BossAmbiguous) as exc:
        await working.edit(embed=make_embed("❓ Unknown Boss", error_text(exc)))
        return
    except Exception as exc:
        await working.edit(embed=make_embed("❌ Sheet Problem", error_text(exc)))
        return

    boss, points = context["boss"], context["points"]
    await working.edit(
        embed=make_embed("\U0001f50e Reading Screenshot", f"Working on **{boss}**...")
    )

    try:
        image_bytes = await attachment.read()
        raw_names = await asyncio.to_thread(
            extract_names, image_bytes, content_type.split(";")[0].strip()
        )
    except VisionError as exc:
        await working.edit(
            embed=make_embed(
                "❌ Couldn't Read That",
                error_text(exc),
                footer="Try a clearer or less cropped screenshot.",
            )
        )
        return
    except Exception as exc:
        await working.edit(
            embed=make_embed("❌ Couldn't Read That", error_text(exc))
        )
        return

    # Hashed here, not from attachment.id: Discord mints a new attachment
    # id on every upload, so id-based duplicate detection never fires on a
    # genuine re-post of the same picture. The hash of the bytes is the
    # same every time, which is what "already logged" needs to mean. Must
    # come after attachment.read() above -- the hash is of the actual
    # image content, not anything derivable before downloading it.
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    try:
        duplicate = await asyncio.to_thread(
            image_already_logged, spreadsheet, image_sha256
        )
    except Exception as exc:
        await working.edit(embed=make_embed("❌ Sheet Problem", error_text(exc)))
        return

    try:
        matched, unmatched = match_names(raw_names, context["players"])
    except DuplicatePlayerName as exc:
        await working.edit(
            embed=make_embed(
                "❌ Duplicate Player Names",
                f"{error_text(exc)}\n\nFix the sheet's player column first.",
            )
        )
        return

    if not matched:
        await working.edit(
            embed=make_embed(
                "❌ No Known Players Found",
                f"None of the names matched a player in **{context['tab']}**."
                f"\n\nRead: {', '.join(raw_names)}",
            )
        )
        return

    players = [m.player for m in matched]
    embed = make_embed(
        "\U0001f4cb Confirm Attendance",
        f"**{boss}** — **+{points}** point{'s' if points != 1 else ''} each, "
        f"into **{context['tab']}**",
        footer=f"React {CONFIRM_EMOJI} within {PREVIEW_TIMEOUT}s to write. "
               "Nothing is saved until you do.",
    )
    embed.add_field(
        name=f"✅ Matched ({len(players)})",
        value="\n".join(players)[:1024],
        inline=False,
    )
    if unmatched:
        embed.add_field(
            name=f"❓ Not Recognised ({len(unmatched)})",
            value=("\n".join(unmatched)[:1024]
                   + "\n\n*Skipped. Add them to the sheet first if they count.*"),
            inline=False,
        )
    if duplicate:
        embed.add_field(
            name="⚠️ Already Logged",
            value="This exact screenshot has been logged before. "
                  "Confirming will add the points again.",
            inline=False,
        )

    await working.edit(embed=embed)
    await working.add_reaction(CONFIRM_EMOJI)

    # Officer-only gates the approval too, not just the command: anyone
    # can react, so a non-officer must not be able to approve someone
    # else's preview. role_id is the one already fetched above.
    def check(reaction, user):
        return (
            reaction.message.id == working.id
            and str(reaction.emoji) == CONFIRM_EMOJI
            and user.id != bot.user.id
            and _is_officer(user, role_id)
        )

    try:
        # Deliberately outside _SHEET_LOCK: holding it here would stall
        # every other command for up to PREVIEW_TIMEOUT seconds.
        _, confirmer = await bot.wait_for(
            "reaction_add", check=check, timeout=PREVIEW_TIMEOUT
        )
    except asyncio.TimeoutError:
        await working.edit(
            embed=make_embed(
                "⏱️ Expired",
                f"No confirmation within {PREVIEW_TIMEOUT}s. Nothing was written.",
            )
        )
        return

    entry = {
        "timestamp": _timestamp(),
        "tab": context["tab"],
        "boss": boss,
        "points_each": points,
        "message_id": str(ctx.message.id),
        "attachment_id": str(attachment.id),
        "image_sha256": image_sha256,
        "confirmed_by": str(confirmer),
        # JSON, not comma-joined -- see _parse_players.
        "players": json.dumps(players),
        "reversed": "",
    }

    try:
        await _locked(
            _commit,
            context["tab"],
            boss,
            players,
            points,
            entry,
            duplicate,
        )
    except PointsWrittenButNotLogged as exc:
        # The points landed and the log row did not. Saying "nothing was
        # written" here is what makes an officer re-run and double-pay --
        # this is the one message that must reach the officer by whatever
        # means necessary.
        await _report_partial_failure(
            ctx,
            working,
            "⚠️ Partly Written — Do Not Re-Run",
            exc.description,
            footer=f"Confirmed by {confirmer}",
        )
        return
    except AlreadyLoggedDuringPreview as exc:
        await working.edit(embed=make_embed("⚠️ Already Logged", error_text(exc)))
        return
    except TimeoutError:
        # LOCK_TIMEOUT fired. The to_thread worker running _commit is NOT
        # stopped by this -- it may already have applied the points and be
        # partway through append_log_entry, or may not have started
        # either. Must NOT be reported as "nothing was written": that is
        # exactly the false certainty that makes an officer re-run and
        # double-pay. Caught explicitly, before the generic Exception
        # branch below (which is only reached when a write genuinely never
        # started), so this case can never fall into "Nothing was written".
        await _report_partial_failure(
            ctx,
            working,
            "⏱️ Sheet Timed Out — Verify Before Re-Running",
            _timeout_description(
                verb="writing",
                outcome="add",
                tab=context["tab"],
                boss=boss,
                points=points,
                players=players,
            ),
            footer=f"Confirmed by {confirmer}",
        )
        return
    except Exception as exc:
        # Reached only if apply_writes itself failed, so nothing landed.
        await working.edit(
            embed=make_embed(
                "❌ Write Failed", f"{error_text(exc)}\n\nNothing was written."
            )
        )
        return

    await working.edit(
        embed=make_embed(
            "✅ Attendance Recorded",
            f"**+{points}** for **{boss}** — {len(players)} player"
            f"{'s' if len(players) != 1 else ''} in **{context['tab']}**.",
            footer=f"Confirmed by {confirmer} • !undoattendance reverses this",
        )
    )


@bot.command(name="undoattendance")
async def undo_attendance_cmd(ctx: commands.Context):
    """Reverse the most recent attendance log: !undoattendance"""
    spreadsheet = await _open_spreadsheet_or_reject(ctx)
    if spreadsheet is None:
        return
    if await _require_officer(ctx, spreadsheet) is None:
        return

    # Best-effort only, read-only, outside the lock: which entry is about
    # to be reversed, so a LOCK_TIMEOUT below can still name it. _locked's
    # own re-read inside the lock is what's actually authoritative; this
    # peek can be stale (or simply fail) without affecting correctness --
    # it only affects how specific the timeout message below can be.
    try:
        pending = await asyncio.to_thread(last_unreversed_entry, spreadsheet)
    except Exception:
        pending = None
    pending_entry = pending[1] if pending is not None else None

    try:
        entry = await _locked(_reverse_last)
    except PointsRemovedButNotMarked as exc:
        # The subtraction landed and the reversed flag did not. "Undo
        # Failed" would invite the retry that subtracts them twice -- this
        # is the one message that must reach the officer by whatever means
        # necessary.
        await _report_partial_failure(
            ctx, None, "⚠️ Partly Undone — Do Not Re-Run", exc.description
        )
        return
    except TimeoutError:
        # Mirror of the attendance_cmd case: the to_thread worker running
        # _reverse_last is not stopped by LOCK_TIMEOUT and may already
        # have subtracted the points and be partway through
        # mark_entry_reversed, or may not have started either. Must not
        # be reported as "nothing was changed".
        players = None
        if pending_entry:
            try:
                players = _parse_players(pending_entry["players"])
            except SheetStructureError:
                players = None  # best-effort only; still report the timeout
        await _report_partial_failure(
            ctx,
            None,
            "⏱️ Sheet Timed Out — Verify Before Re-Running",
            _timeout_description(
                verb="undoing the last attendance entry",
                outcome="remove",
                tab=pending_entry["tab"] if pending_entry else None,
                boss=pending_entry["boss"] if pending_entry else None,
                points=(
                    int(pending_entry["points_each"]) if pending_entry else None
                ),
                players=players,
            ),
        )
        return
    except Exception as exc:
        # Reached only if the subtraction itself failed, so nothing changed.
        await _reject(
            ctx, "❌ Undo Failed", f"{error_text(exc)}\n\nNothing was changed."
        )
        return

    if entry is None:
        await _reject(ctx, "ℹ️ Nothing To Undo", "No attendance log found.")
        return

    await ctx.send(
        embed=make_embed(
            "↩️ Attendance Reversed",
            f"Removed **{entry['points_each']}** points for "
            f"**{entry['boss']}** from **{entry['tab']}**.",
            footer=f"Originally logged {entry['timestamp']} "
                   f"by {entry['confirmed_by']}",
        )
    )


@bot.command(name="setweek")
async def set_week_cmd(ctx: commands.Context, *, tab_name: str = ""):
    """Choose which sheet tab attendance goes into: !setweek Week 17.1"""
    spreadsheet = await _open_spreadsheet_or_reject(ctx)
    if spreadsheet is None:
        return
    if await _require_officer(ctx, spreadsheet) is None:
        return

    tab = tab_name.strip()
    if not tab:
        await _reject(
            ctx, "❓ Which Tab?", "Usage: `!setweek <tab name>`",
            footer="e.g. !setweek Week 17.1",
        )
        return

    try:
        await _locked(_set_target_tab, tab)
    except Exception as exc:
        await _reject(
            ctx,
            "❌ No Such Tab",
            f"Couldn't open a tab named `{tab}`.\n\n{error_text(exc)}",
        )
        return

    await ctx.send(
        embed=make_embed(
            "✅ Target Tab Set", f"Attendance will be written to **{tab}**."
        )
    )


@bot.command(name="setofficerrole")
@commands.has_permissions(administrator=True)
async def set_officer_role_cmd(ctx: commands.Context, role: discord.Role):
    """Choose which role may log attendance: !setofficerrole @Officer"""
    try:
        await _locked(_set_officer_role, str(role.id))
    except Exception as exc:
        await _reject(ctx, "❌ Sheet Problem", error_text(exc))
        return

    await ctx.send(
        embed=make_embed(
            "✅ Officer Role Set", f"{role.mention} can now record attendance."
        )
    )


@bot.command(name="attendancehelp")
async def attendance_help_cmd(ctx: commands.Context):
    """Show the attendance commands: !attendancehelp"""
    embed = make_embed(
        "\U0001f4cb Attendance Commands",
        "Log guild attendance from an in-game roster screenshot.",
        footer="Points are added to the Point System sheet",
    )
    embed.add_field(
        name="!attendance <boss>",
        value="Attach a roster screenshot. Officers only.", inline=False,
    )
    embed.add_field(
        name="!undoattendance",
        value="Reverse the last log. Officers only.", inline=False,
    )
    embed.add_field(
        name="!setweek <tab>",
        value="Set the target tab, e.g. `Week 17.1`.", inline=False,
    )
    embed.add_field(
        name="!setofficerrole @role",
        value="Admins only. Sets who may log.", inline=False,
    )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    missing = [
        name
        for name, value in (
            ("ATTENDANCE_DISCORD_TOKEN", TOKEN),
            ("SHEET_ID", SHEET_ID),
            ("GOOGLE_SERVICE_ACCOUNT_JSON", SERVICE_ACCOUNT_JSON),
            ("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY")),
        )
        if not value
    ]
    if missing:
        # Exit deliberately so the supervisor leaves this stopped instead
        # of crash-looping. The timer is unaffected.
        print(
            "Attendance bot not started; missing: " + ", ".join(missing),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_NOT_CONFIGURED)

    bot.run(TOKEN)
