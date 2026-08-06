"""Attendance logging bot for the Lordnine guild.

A second Discord application, separate from the field boss timer. It does
not import bot.py, share its token, or run in its process -- the timer is
in production and must not be affected by anything here.

Started by supervisor.py. Exits with EXIT_NOT_CONFIGURED when its
credentials are absent, which the supervisor treats as a deliberate stop,
so the timer runs normally on a deploy that predates these secrets.
"""

import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from dotenv import load_dotenv

from attendance_bosses import BossAmbiguous, BossNotFound, boss_points, resolve_boss
from attendance_roster import match_names
from attendance_sheet import (
    SheetStructureError,
    append_log_entry,
    apply_writes,
    attachment_already_logged,
    find_column,
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
            f"**+{self.points}** for **{self.boss}** **were added** to "
            f"**{self.tab}** for {len(self.players)} player"
            f"{'s' if len(self.players) != 1 else ''}, but the audit log row "
            f"could not be written.\n\n"
            f"Please do **not re-run** this command — the points are already "
            f"in the sheet and running it again would add them twice. "
            f"`!undoattendance` cannot reverse this either, because there is "
            f"no log row for it; it must be reconciled by hand.\n\n"
            f"Players: {', '.join(self.players)}\n\nReason: {self.cause_text}"
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
        return (
            f"**{self.entry['points_each']}** points for "
            f"**{self.entry['boss']}** have **already** been removed from "
            f"**{self.entry['tab']}**, but the log entry could not be marked "
            f"reversed.\n\n"
            f"Please do **not re-run** `!undoattendance` — the entry still "
            f"looks live, so running it again would remove those points "
            f"twice. Mark the row reversed by hand instead.\n\n"
            f"Players: {self.entry['players']}\n\nReason: {self.cause_text}"
        )


class AlreadyLoggedDuringPreview(RuntimeError):
    """Another officer confirmed this same screenshot during the 180s wait."""


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


def _officer_role_id() -> int | None:
    raw = read_config(_spreadsheet()).get("officer_role_id", "")
    return int(raw) if raw.isdigit() else None


def _is_officer(member, role_id: int | None) -> bool:
    if role_id is None:
        return False
    return any(role.id == role_id for role in getattr(member, "roles", []))


def _load_context(boss_query: str, attachment_id: str) -> dict:
    """Everything the preview needs, in one trip to Sheets.

    Read-only, so it does not need _SHEET_LOCK: nothing it returns is
    written back without a fresh read inside the locked commit.
    """
    spreadsheet = _spreadsheet()
    config = read_config(spreadsheet)

    tab = config.get("target_tab")
    if not tab:
        raise SheetStructureError("No target tab set. Run !setweek <tab name> first.")

    worksheet = spreadsheet.worksheet(tab)
    boss = resolve_boss(read_headers(worksheet), boss_query)

    # find_column's result is deliberately discarded: calling it here only
    # fails fast, before a Gemini request, if the boss has no column or
    # has two. The index itself must not be carried across the preview --
    # see _commit.
    find_column(worksheet, boss)

    return {
        "tab": tab,
        "boss": boss,
        "points": boss_points(boss),
        "players": read_players(worksheet),
        "duplicate": attachment_already_logged(spreadsheet, attachment_id),
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
    """
    spreadsheet = _spreadsheet()

    if not was_duplicate and attachment_already_logged(
        spreadsheet, entry["attachment_id"]
    ):
        raise AlreadyLoggedDuringPreview(
            "This screenshot was logged by someone else while this preview "
            "was open. Nothing was written, to avoid paying twice."
        )

    worksheet = spreadsheet.worksheet(tab)
    column = find_column(worksheet, boss)
    apply_writes(worksheet, plan_point_writes(worksheet, players, column, points))

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
    players = [p.strip() for p in entry["players"].split(",") if p.strip()]
    worksheet = spreadsheet.worksheet(entry["tab"])
    # Resolved by name inside the lock, same reasoning as _commit.
    column = find_column(worksheet, entry["boss"])

    apply_writes(
        worksheet,
        plan_point_writes(worksheet, players, column, -int(entry["points_each"])),
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


async def _locked(func, *args):
    """Run one blocking sheet mutation as a single serialised unit.

    The lock covers the whole read-plan-write section, not the individual
    gspread calls, and is released the moment the work is done.
    """
    async with _SHEET_LOCK:
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


async def _require_officer(ctx) -> int | None:
    """The configured officer role id, or None once a refusal has been sent."""
    try:
        role_id = await asyncio.to_thread(_officer_role_id)
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
    role_id = await _require_officer(ctx)
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
        context = await asyncio.to_thread(_load_context, boss_name, str(attachment.id))
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

    matched, unmatched = match_names(raw_names, context["players"])
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
    if context["duplicate"]:
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
        "confirmed_by": str(confirmer),
        "players": ", ".join(players),
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
            context["duplicate"],
        )
    except PointsWrittenButNotLogged as exc:
        # The points landed and the log row did not. Saying "nothing was
        # written" here is what makes an officer re-run and double-pay.
        await working.edit(
            embed=make_embed(
                "⚠️ Partly Written — Do Not Re-Run",
                exc.description,
                footer=f"Confirmed by {confirmer}",
            )
        )
        return
    except AlreadyLoggedDuringPreview as exc:
        await working.edit(embed=make_embed("⚠️ Already Logged", error_text(exc)))
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
    if await _require_officer(ctx) is None:
        return

    try:
        entry = await _locked(_reverse_last)
    except PointsRemovedButNotMarked as exc:
        # The subtraction landed and the reversed flag did not. "Undo
        # Failed" would invite the retry that subtracts them twice.
        await _reject(ctx, "⚠️ Partly Undone — Do Not Re-Run", exc.description)
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
    if await _require_officer(ctx) is None:
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
