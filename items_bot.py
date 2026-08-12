"""Discord bot for guild item requests and officer distribution.

A third process alongside the timer and the attendance bot, with its own
Discord token and its own spreadsheet, so nothing it does can affect
either of them. See supervisor.py for why all three share one Render
service.

Authorization has two shapes. !distribute is accepted only in the private
officer channel, so a button there can only be pressed by someone Discord
already lets see the channel. The raffle commands cannot work that way --
the poll must be visible to members -- so they are gated on configured
roles instead.
"""

import asyncio
import datetime
import os
import sys
from difflib import get_close_matches

import discord
from discord.ext import commands
from dotenv import load_dotenv

import channel_guard
import items_rules
import items_raffle
import items_board
import items_sheet
import items_state

load_dotenv()

EXIT_NOT_CONFIGURED = 78

REQUIRED_ENV = ("ITEMS_DISCORD_TOKEN", "ITEMS_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")

# How long a !distribute panel accepts clicks. The queue outlives the
# panel; expiry only means the officer re-runs the command.
PANEL_TIMEOUT = 900  # 15 minutes

# Reposting is presentation-only, so losing this cadence across a restart is
# harmless and does not justify expanding the persisted queue-state schema.
BOARD_REPOST_EVERY = 3
_SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED = 0

# Serializes every read-then-write pair. Two officers approving at once
# would otherwise both read "2 used today" and both write, yielding 4.
_SHEET_LOCK = asyncio.Lock()

_STATE = items_state.State()

# The pinned messages holding _STATE, cached in shard order so save_state
# edits them instead of posting a new copy on every change.
_STATE_MESSAGES: list[discord.Message] = []

_SPREADSHEET = None

intents = discord.Intents.default()
intents.message_content = True
# Poll voters arrive as Members only when this intent is on; otherwise
# discord.py yields Users, whose display_name is the global name rather
# than the 'BK | Jjew' server nickname the roster match reads. It is a
# privileged intent and must also be enabled for this application in the
# Discord Developer Portal.
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def missing_credentials(env: dict) -> list[str]:
    return [name for name in REQUIRED_ENV if not env.get(name)]


def today_pht() -> str:
    """Today's date in Manila, as the ledger writes it."""
    return items_rules.pht_day(items_rules.format_timestamp(items_rules.now_pht()))


def gear_cap() -> int:
    """The daily gear limit, from the environment.

    A malformed value falls back to the default rather than crashing the
    bot: a typo in a Render env var should not take the bot down.
    """
    try:
        return int(os.getenv("ITEMS_GEAR_DAILY_CAP", ""))
    except ValueError:
        return items_rules.DEFAULT_GEAR_DAILY_CAP


async def save_state(channel) -> None:
    """Write _STATE into its pinned message shards."""
    global _STATE_MESSAGES
    try:
        contents = items_state.encode_state(_STATE)
    except ValueError as exc:
        print(f"[items] could not save state: {exc!r}", file=sys.stderr, flush=True)
        await channel.send(
            embed=error_embed(
                "State could not be saved",
                "The request queue is too large to store safely. An officer must "
                "work the queue down before more requests can be accepted.",
            )
        )
        return

    messages: list[discord.Message] = []
    for index, content in enumerate(contents):
        if index < len(_STATE_MESSAGES):
            message = _STATE_MESSAGES[index]
            # A member request holds _SHEET_LOCK; rewriting every unchanged
            # shard here amplifies Discord rate limits and makes that path wait.
            if (
                message.content
                and message.content.startswith(items_state.STATE_MARKER)
                and message.content == content
            ):
                messages.append(message)
                continue
            try:
                await message.edit(content=content)
                messages.append(message)
                continue
            except discord.HTTPException:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

        message = await channel.send(content)
        try:
            await message.pin()
        except discord.HTTPException:
            # Pinning needs Manage Messages. Without it the message still
            # works -- load_state scans history too -- so this is not fatal.
            pass
        messages.append(message)

    for message in _STATE_MESSAGES[len(contents) :]:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    _STATE_MESSAGES = messages
    _STATE.missing_parts = ()


async def load_state(channel) -> bool:
    """Restore _STATE from the channel's pinned messages.

    Returns True if a state message was found.

    channel.pins() is an ASYNC ITERATOR in discord.py 2.7, not a
    coroutine returning a list -- `await channel.pins()` raises
    TypeError. It must be consumed with `async for`.
    """
    global _STATE, _STATE_MESSAGES
    candidates = [
        message
        async for message in channel.pins(limit=50)
        if message.author.bot
    ]
    candidates += [
        message
        async for message in channel.history(limit=100)
        if message.author.bot
    ]
    unique = {message.id: message for message in candidates}
    shard_messages = [
        (decoded, message)
        for message in unique.values()
        if (decoded := items_state.decode_state(message.content)) is not None
    ]
    shard_by_part: dict[int, tuple[items_state.Shard, discord.Message]] = {}
    stale_messages: list[discord.Message] = []
    for decoded, message in shard_messages:
        previous = shard_by_part.get(decoded.part)
        if previous is None or message.id > previous[1].id:
            if previous is not None:
                stale_messages.append(previous[1])
            # A replacement has a newer Discord snowflake, so it is the
            # authoritative shard when an old edit-failure orphan remains.
            shard_by_part[decoded.part] = (decoded, message)
        else:
            stale_messages.append(message)
    for message in stale_messages:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    shard_messages = list(shard_by_part.values())
    part_zero = shard_by_part.get(0)
    if part_zero is not None:
        # Message index 0 is edited on every save and is only deleted as
        # surplus when the state has no shards, so its newest copy defines
        # the current generation.
        authoritative_total = part_zero[0].total
        obsolete_messages = [
            message
            for decoded, message in shard_messages
            if decoded.part >= authoritative_total
        ]
        for message in obsolete_messages:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        shard_messages = [
            (decoded, message)
            for decoded, message in shard_messages
            if decoded.part < authoritative_total
        ]
    restored = items_state.decode_shards(
        [message.content for _, message in shard_messages]
    )
    if restored is None:
        return False

    _STATE = restored
    _STATE.officer_channel_id = _STATE.officer_channel_id or channel.id
    _STATE_MESSAGES = [
        message for _, message in sorted(shard_messages, key=lambda pair: pair[0].part)
    ]
    if _STATE.missing_parts:
        await channel.send(
            embed=error_embed(
                "State recovery incomplete",
                f"Recovered what could be read, but {len(_STATE.missing_parts)} "
                "state shard(s) are missing. Check this channel's pinned messages.",
            )
        )
    return True


def _embed(title: str, description: str, colour: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=colour)


def ok_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"✅ {title}", description, 0x2ECC71)


def error_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"❌ {title}", description, 0xE74C3C)


def warn_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"⚠️ {title}", description, 0xF1C40F)


def is_officer_channel(channel_id: int) -> bool:
    return (
        _STATE.officer_channel_id is not None
        and channel_id == _STATE.officer_channel_id
    )


async def refresh_board() -> None:
    """Redraw the member-facing queue board when one is configured."""
    global _SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED
    if _STATE.queue_channel_id is None:
        return
    try:
        channel = bot.get_channel(_STATE.queue_channel_id)
        if channel is None:
            raise LookupError("configured queue channel is unreachable")
        embed = _embed("📦 Queue Board", items_board.render_board(_STATE.queue), 0x3498DB)
        message = None
        if _STATE.board_message_id is not None:
            try:
                message = await channel.fetch_message(_STATE.board_message_id)
            except discord.NotFound:
                pass
        repost = _SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED >= BOARD_REPOST_EVERY
        if message is not None and not repost:
            try:
                await message.edit(embed=embed)
                # The board content matters even when pinning is temporarily
                # unavailable, so retry the best-effort pin only after editing.
                if not message.pinned:
                    try:
                        await message.pin()
                    except Exception as exc:
                        print(
                            f"[items] could not pin queue board: {exc!r}",
                            file=sys.stderr,
                            flush=True,
                        )
                return
            except discord.NotFound:
                pass

        if message is not None and repost:
            try:
                await message.delete()
            except Exception as exc:
                print(
                    f"[items] could not remove old queue board: {exc!r}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                # A replacement send may fail, so never retain an ID for a
                # message we know Discord has already removed.
                _STATE.board_message_id = None

        message = await channel.send(embed=embed)
        try:
            await message.pin()
        except Exception as exc:
            print(f"[items] could not pin queue board: {exc!r}", file=sys.stderr, flush=True)
        _STATE.board_message_id = message.id
        _SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED = 0
        state_channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if state_channel is None:
            raise LookupError("configured officer channel is unreachable")
        # Saving happens only after a replacement has a real ID. save_state()
        # never refreshes the board, so this persistence cannot recurse.
        await save_state(state_channel)
    except Exception as exc:
        # The board is cosmetic: it must never turn a completed queue change
        # into a failed request or, worse, a seemingly failed sheet approval.
        print(f"[items] could not refresh queue board: {exc!r}", file=sys.stderr, flush=True)


@bot.command(name="setofficerchannel")
@commands.has_permissions(administrator=True)
async def setofficerchannel_cmd(ctx):
    """Record this channel as the officers' channel."""
    global _STATE_MESSAGES
    if _STATE.officer_channel_id != ctx.channel.id:
        _STATE_MESSAGES = []
    _STATE.officer_channel_id = ctx.channel.id
    await save_state(ctx.channel)
    await ctx.send(
        embed=ok_embed(
            "Officer channel set",
            f"`!distribute` now works in {ctx.channel.mention}, and the bot "
            "keeps its request queue in pinned messages here. A long queue "
            "needs several of them. Don't delete any.",
        )
    )


@bot.command(name="setqueuechannel")
@commands.has_permissions(administrator=True)
async def setqueuechannel_cmd(ctx):
    """Record this channel as the member-facing queue board."""
    if _STATE.officer_channel_id is None:
        await ctx.send(
            embed=error_embed(
                "Not set up yet",
                "An admin must run `!setofficerchannel` in the officers' "
                "channel before a queue board can be set.",
            )
        )
        return

    previous_channel = (
        bot.get_channel(_STATE.queue_channel_id)
        if _STATE.queue_channel_id is not None
        else None
    )
    if previous_channel is not None and _STATE.board_message_id is not None:
        try:
            message = await previous_channel.fetch_message(_STATE.board_message_id)
            await message.delete()
        except Exception as exc:
            # Clearing the old board is tidiness, not correctness: moving the
            # board must succeed even when the previous one cannot be removed.
            print(f"[items] could not remove old queue board: {exc!r}", file=sys.stderr, flush=True)

    _STATE.queue_channel_id = ctx.channel.id
    _STATE.board_message_id = None
    state_channel = (
        bot.get_channel(_STATE.officer_channel_id)
        if _STATE.officer_channel_id is not None
        else None
    )
    if state_channel is not None:
        # Persist the destination before drawing: a board failure must not undo
        # an admin's configuration. refresh_board saves once more only for a
        # newly created message ID, and save_state never calls it back.
        await save_state(state_channel)
    await refresh_board()
    await ctx.send(
        embed=ok_embed(
            "Queue channel set",
            f"The member queue board is now in {ctx.channel.mention}. The bot "
            "will keep it updated and pinned here.",
        )
    )


# raffle_access returns this instead of a message when the command should
# produce no reply at all. A distinct object rather than None, because
# None already means "permitted".
IGNORE = "\x00ignore"


def has_raffle_role(author, role_ids: list[int]) -> bool:
    """True if the author holds ANY configured raffle role."""
    wanted = set(role_ids or ())
    if not wanted:
        return False
    return any(role.id in wanted for role in getattr(author, "roles", []))


def raffle_access(ctx) -> str | None:
    """None if this raffle command may run, else a refusal or IGNORE.

    Wrong-channel is silent: a raffle command typed elsewhere is far more
    likely to be a typo than an attack, and a reply would only advertise
    that the channel exists. The unconfigured case is the exception --
    silence there is a dead end, so the one person who can fix it is told
    and nobody else is.
    """
    if _STATE.raffle_channel_id is None:
        permissions = getattr(ctx.author, "guild_permissions", None)
        if getattr(permissions, "administrator", False):
            return (
                "No raffle channel is set. Run `!setrafflechannel` in the "
                "channel where special log polls should be posted."
            )
        return IGNORE

    if ctx.channel.id != _STATE.raffle_channel_id:
        return IGNORE

    if not _STATE.raffle_role_ids:
        return (
            "No raffle role is set. An admin must run "
            "`!setraffleroles @role` before the raffle commands work."
        )
    if not has_raffle_role(ctx.author, _STATE.raffle_role_ids):
        return "You need a raffle role to run this command."
    return None


async def _refuse_raffle(ctx, verdict: str) -> bool:
    """Send the refusal if there is one. True when the caller must stop."""
    if verdict is None:
        return False
    if verdict is not IGNORE:
        await ctx.send(embed=error_embed("Not allowed", verdict))
    return True


# Which channel each command belongs in. The !set*channel commands are
# exempt because they are how a channel is chosen; everything else is
# confined to the channel matching its audience -- members to the queue
# board, officers to the private officer channel, raffles to the raffle
# channel. Until the relevant channel is set the entry resolves to None
# and channel_guard leaves the command unrestricted.
_EXEMPT_COMMANDS = frozenset({
    "setofficerchannel", "setqueuechannel", "setrafflechannel",
})
_OFFICER_COMMANDS = frozenset({"distribute", "setraffleroles"})
_RAFFLE_COMMANDS = frozenset({"poll", "list", "winner", "cancelpoll"})
_QUEUE_COMMANDS = frozenset({"request", "cancelrequest", "myrequests", "itemhelp"})

_CLASSIFIED_COMMANDS = (
    _EXEMPT_COMMANDS | _OFFICER_COMMANDS | _RAFFLE_COMMANDS | _QUEUE_COMMANDS
)


def command_channels(ctx):
    """The channel ids ctx.command may run in, or EXEMPT."""
    name = ctx.command.name
    if name in _OFFICER_COMMANDS:
        return (_STATE.officer_channel_id,)
    if name in _RAFFLE_COMMANDS:
        return (_STATE.raffle_channel_id,)
    if name in _QUEUE_COMMANDS:
        return (_STATE.queue_channel_id,)
    # Exempt, and the deliberate default for anything unclassified: an
    # unlisted command keeps working everywhere rather than dying
    # silently. test_every_registered_command_is_classified is what stops
    # that default from quietly swallowing a new command.
    return channel_guard.EXEMPT


bot.add_check(channel_guard.make_check(command_channels))


@bot.command(name="setraffleroles")
@commands.has_permissions(administrator=True)
async def setraffleroles_cmd(ctx, *roles: discord.Role):
    """Choose which roles may run the raffle commands."""
    if not roles:
        await ctx.send(
            embed=error_embed(
                "Which role?",
                "Usage: `!setraffleroles @role [@role ...]`\n"
                "Every role you list replaces the current set.",
            )
        )
        return

    # Deduplicated by id, order preserved, so mentioning a role twice
    # does not store it twice. Keyed on the id rather than the object so
    # this never depends on Role being hashable.
    unique: dict[int, discord.Role] = {}
    for role in roles:
        unique.setdefault(role.id, role)

    # Under the lock like every other state write: without it this can
    # encode a raffle mid-draw, suspend inside message.edit, and land
    # last -- persisting a winner-less copy of a raffle already ticked
    # into the sheet.
    async with _SHEET_LOCK:
        previous = list(_STATE.raffle_role_ids)
        _STATE.raffle_role_ids = list(unique)
        if not items_state.fits(_STATE):
            _STATE.raffle_role_ids = previous
            await ctx.send(
                embed=error_embed(
                    "Too many roles",
                    f"{len(unique)} roles will not fit in the bot's storage. "
                    "Name fewer roles, or give one role to everyone who runs "
                    "the raffle.",
                )
            )
            return

        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)
    mentions = ", ".join(role.mention for role in unique.values())
    await ctx.send(
        embed=ok_embed(
            "Raffle roles set",
            f"{mentions} can now run `!poll`, `!list` and `!winner`.",
        )
    )


@bot.command(name="setrafflechannel")
@commands.has_permissions(administrator=True)
async def setrafflechannel_cmd(ctx):
    """Record this channel as the special log raffle channel."""
    if _STATE.officer_channel_id is None:
        await ctx.send(
            embed=error_embed(
                "Not set up yet",
                "An admin must run `!setofficerchannel` in the officers' "
                "channel before a raffle channel can be set.",
            )
        )
        return

    # Under the lock for the same reason as !setraffleroles: a config
    # save that interleaves with a draw must not persist a stale copy.
    async with _SHEET_LOCK:
        _STATE.raffle_channel_id = ctx.channel.id
        channel = bot.get_channel(_STATE.officer_channel_id)
        if channel is not None:
            await save_state(channel)
    await ctx.send(
        embed=ok_embed(
            "Raffle channel set",
            f"`!poll`, `!list` and `!winner` now work in {ctx.channel.mention}.",
        )
    )


from dataclasses import dataclass


@dataclass(frozen=True)
class RequestOutcome:
    accepted: bool
    message: str
    request: items_state.PendingRequest | None = None


def evaluate_request(
    argument: str,
    user_id: int,
    snapshot: items_sheet.Snapshot,
    state: items_state.State,
    *,
    cap: int,
    today: str,
) -> RequestOutcome:
    """Decide a !request without touching Discord or the network.

    Pure: the snapshot already carries the special-log checkbox grid, so
    every question this asks is answered from values passed in. That is
    what makes the whole request path testable without a network.
    """
    try:
        parsed = items_rules.parse_request(
            argument, snapshot.roster, snapshot.special_headers, snapshot.gear_headers
        )
    except (items_rules.RequestParseError, items_rules.ItemLookupError) as exc:
        return RequestOutcome(accepted=False, message=str(exc))

    # A member requesting under a different IGN than last time is NOT
    # refused -- requesting for an alt is legitimate. It is flagged for
    # the officer instead, who is the one with the standing to judge it.
    # Blocking here would punish the honest case to catch a typo that
    # the roster check has already largely prevented.
    note = ""
    remembered = state.igns.get(str(user_id))
    if remembered and items_rules.normalize(remembered) != items_rules.normalize(parsed.ign):
        note = f"previously requested as {remembered}"

    # Keyed on IGN, not on the requesting account: the same item must
    # not sit in the queue twice for one player, whoever asked.
    for queued in state.queue:
        if (
            items_rules.normalize(queued.ign) == items_rules.normalize(parsed.ign)
            and items_rules.normalize(queued.item) == items_rules.normalize(parsed.item.name)
        ):
            return RequestOutcome(
                accepted=False,
                message=f"**{parsed.item.name}** is already pending for **{parsed.ign}**.",
            )

    eligibility = items_rules.check_eligibility(
        parsed.item.type,
        parsed.ign,
        snapshot.ledger_rows,
        today,
        already_has_special=items_sheet.holds_special(
            snapshot, parsed.ign, parsed.item.name
        ),
        pending_gear=items_state.pending_gear_for(state, parsed.ign, today),
        cap=cap,
    )
    if not eligibility.allowed:
        return RequestOutcome(
            accepted=False, message=f"**{parsed.ign}** {eligibility.reason}."
        )

    return RequestOutcome(
        accepted=True,
        message=(
            f"Requested **{parsed.item.name}** ({parsed.item.type}) for "
            f"**{parsed.ign}**. An officer will review it."
        ),
        request=items_state.PendingRequest(
            id=items_state.new_request_id(),
            user_id=user_id,
            ign=parsed.ign,
            item=parsed.item.name,
            type=parsed.item.type,
            requested_at=items_rules.format_timestamp(items_rules.now_pht()),
            note=note,
        ),
    )


@bot.command(name="request")
async def request_cmd(ctx, *, argument: str = ""):
    """Ask an officer for an item."""
    global _SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED
    if _STATE.officer_channel_id is None:
        await ctx.send(
            embed=error_embed(
                "Not set up yet",
                "An admin must run `!setofficerchannel` in the officers' "
                "channel before requests can be taken.",
            )
        )
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        outcome = evaluate_request(
            argument,
            ctx.author.id,
            snapshot,
            _STATE,
            cap=gear_cap(),
            today=today_pht(),
        )

        if not outcome.accepted:
            await ctx.send(embed=error_embed("Request refused", outcome.message))
            return

        previous_ign = _STATE.igns.get(str(ctx.author.id))
        _STATE.queue.append(outcome.request)
        _STATE.igns[str(ctx.author.id)] = outcome.request.ign

        if not items_state.fits(_STATE):
            _STATE.queue.remove(outcome.request)
            if previous_ign is None:
                _STATE.igns.pop(str(ctx.author.id), None)
            else:
                _STATE.igns[str(ctx.author.id)] = previous_ign
            await ctx.send(
                embed=error_embed(
                    "Queue is full",
                    "The officers need to work the queue down before your request "
                    "can be recorded. Please try again shortly.",
                )
            )
            return

        channel = bot.get_channel(_STATE.officer_channel_id)
        if channel is None:
            _STATE.queue.remove(outcome.request)
            if previous_ign is None:
                _STATE.igns.pop(str(ctx.author.id), None)
            else:
                _STATE.igns[str(ctx.author.id)] = previous_ign
            await ctx.send(
                embed=error_embed(
                    "Officer channel unreachable",
                    "Your request was not recorded. Please try again after an "
                    "admin restores the officer channel.",
                )
            )
            return
        await save_state(channel)
        _SUCCESSFUL_REQUESTS_SINCE_BOARD_POSTED += 1
        await refresh_board()

    await ctx.send(embed=ok_embed("Request queued", outcome.message))


# Discord allows at most 25 options in a select menu.
MAX_PANEL_OPTIONS = 25


def panel_lines(
    requests: list[items_state.PendingRequest],
    snapshot: items_sheet.Snapshot,
    cap: int,
    today: str,
    start: int = 1,
) -> list[str]:
    """One display line per pending request, with its current standing.

    The standing is recomputed at render time, not stored: an officer
    needs to see the position as it is now, which may differ from when
    the member requested.
    """
    lines = []
    for number, request in enumerate(requests, start=start):
        if request.type == items_rules.GEAR:
            used = items_rules.gear_used_today(snapshot.ledger_rows, request.ign, today)
            flag = "⚠️" if used >= cap else "✅"
            status = f"{flag} {used}/{cap} today"
        elif items_sheet.holds_special(snapshot, request.ign, request.item):
            status = "⚠️ already has it"
        else:
            status = "✅ eligible"
        line = (
            f"**{number}. {request.ign}** — {request.item}  "
            f"`[{request.type}]`  {status}"
        )
        if request.note:
            line += f"\n     ⚠️ {request.note}"
        lines.append(line)
    return lines


def build_panel_embed(
    requests: list[items_state.PendingRequest],
    snapshot: items_sheet.Snapshot,
    cap: int,
    today: str,
    start: int = 1,
) -> discord.Embed:
    if not requests:
        return _embed("📦 Pending Item Requests", "There are no pending requests.", 0x95A5A6)
    body = "\n".join(panel_lines(requests, snapshot, cap, today, start))
    return _embed("📦 Pending Item Requests", body, 0x3498DB)


async def deny(request_id: str) -> str:
    """Drop a request. Writes nothing to any tab."""
    async with _SHEET_LOCK:
        removed = items_state.remove_request(_STATE, request_id)
        if removed is None:
            return "That request was already handled by another officer."
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)
        await refresh_board()
    return f"Denied **{removed.item}** for **{removed.ign}**. Nothing was written to the sheet."


async def approve(request_id: str, officer_name: str) -> str:
    """Write the item to the sheet and the ledger, then drop the request.

    The whole sequence -- re-read, re-check the cap, write -- happens
    under _SHEET_LOCK. Splitting it would let two officers both read
    "2 used today" and both write.
    """
    async with _SHEET_LOCK:
        request = items_state.find_request(_STATE, request_id)
        if request is None:
            return "That request was already handled by another officer."

        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            return f"Could not read the sheet, so nothing was written: {exc}"

        if items_sheet.already_recorded(snapshot, request.id):
            items_state.remove_request(_STATE, request_id)
            channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
            if channel is not None:
                await save_state(channel)
            await refresh_board()
            return (
                f"**{request.item}** for **{request.ign}** was already recorded. "
                "Nothing was written again."
            )

        eligibility = items_rules.check_eligibility(
            request.type,
            request.ign,
            snapshot.ledger_rows,
            today_pht(),
            already_has_special=items_sheet.holds_special(
                snapshot, request.ign, request.item
            ),
            cap=gear_cap(),
        )
        if not eligibility.allowed:
            return (
                f"Not approved: **{request.ign}** {eligibility.reason}. "
                "The request is still in the queue."
            )

        try:
            await asyncio.to_thread(
                lambda: items_sheet.commit_approval(
                    _SPREADSHEET,
                    ign=request.ign,
                    item=request.item,
                    item_type=request.type,
                    timestamp=items_rules.format_timestamp(items_rules.now_pht()),
                    officer=officer_name,
                    user_id=request.user_id,
                    request_id=request.id,
                )
            )
        except items_sheet.LedgerWriteError as exc:
            # The item cell IS written. Retrying would double-count a
            # gear increment and could never succeed for a special log,
            # so drop the request and hand the officers the exact row.
            items_state.remove_request(_STATE, request_id)
            channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
            if channel is not None:
                await save_state(channel)
            await refresh_board()
            pasteable = " | ".join(exc.row)
            return (
                f"⚠️ **{request.item}** was given to **{request.ign}** "
                f"(cell {exc.address} is updated), but the Distribution Log "
                f"row could not be written: {exc}\n"
                f"Do NOT approve this again — add this row to "
                f"`{items_sheet.LEDGER_TAB}` by hand:\n```\n{pasteable}\n```"
            )
        except Exception as exc:
            return f"Sheet write failed, request kept in the queue: {exc}"

        items_state.remove_request(_STATE, request_id)
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)
        await refresh_board()

    return f"Approved **{request.item}** for **{request.ign}**."


def page_count(requests: list[items_state.PendingRequest]) -> int:
    return max(1, (len(requests) + MAX_PANEL_OPTIONS - 1) // MAX_PANEL_OPTIONS)


class DistributePanel(discord.ui.View):
    """Select a request, then approve or deny it.

    A select plus two buttons rather than a pair of buttons per request:
    Discord allows five action rows of five components, which would cap
    the panel at five requests. A select handles 25.
    """

    def __init__(
        self,
        requests: list[items_state.PendingRequest],
        snapshot: items_sheet.Snapshot,
        *,
        cap: int,
        today: str,
        page: int = 0,
    ):
        super().__init__(timeout=PANEL_TIMEOUT)
        # A panel is shared by several officers at once; one officer's
        # dropdown choice must never become another officer's approval.
        self.selected: dict[int, str] = {}
        self.requests = list(requests)
        self.snapshot = snapshot
        self.cap = cap
        self.today = today
        self.total_pages = page_count(self.requests)
        self.page = min(max(page, 0), self.total_pages - 1)
        self.start = self.page * MAX_PANEL_OPTIONS + 1
        # Set by the sender so on_timeout can edit the panel.
        self.message: discord.Message | None = None

        page_requests = self.requests[
            self.page * MAX_PANEL_OPTIONS : (self.page + 1) * MAX_PANEL_OPTIONS
        ]
        options = [
            discord.SelectOption(
                label=f"{n}. {r.ign} — {r.item}"[:100],
                value=r.id,
                description=f"{r.type} · requested {r.requested_at}"[:100],
            )
            for n, r in enumerate(page_requests, start=self.start)
        ]
        self.picker = discord.ui.Select(
            placeholder="Choose a request…", options=options, row=0
        )
        self.picker.callback = self._on_pick
        self.add_item(self.picker)
        self._add_page_controls()

    def build_embed(self) -> discord.Embed:
        requests = self.requests[
            self.page * MAX_PANEL_OPTIONS : (self.page + 1) * MAX_PANEL_OPTIONS
        ]
        embed = build_panel_embed(
            requests, self.snapshot, self.cap, self.today, start=self.start
        )
        if self.total_pages > 1:
            embed.set_footer(text=f"Page {self.page + 1} of {self.total_pages}")
        return embed

    def _add_page_controls(self) -> None:
        if self.total_pages == 1:
            return

        if self.total_pages > 5:
            self._add_page_button("◀", self.page - 1, disabled=self.page == 0)
            if self.page == 0:
                numbers = range(0, 3)
            elif self.page == self.total_pages - 1:
                numbers = range(self.total_pages - 3, self.total_pages)
            else:
                numbers = range(self.page - 1, self.page + 2)
            for page in numbers:
                self._add_page_button(str(page + 1), page, disabled=page == self.page)
            self._add_page_button(
                "▶", self.page + 1, disabled=self.page == self.total_pages - 1
            )
            return

        for page in range(self.total_pages):
            self._add_page_button(str(page + 1), page, disabled=page == self.page)

    def _add_page_button(self, label: str, page: int, *, disabled: bool) -> None:
        button = discord.ui.Button(
            label=label,
            style=(
                discord.ButtonStyle.primary
                if page == self.page and label not in {"◀", "▶"}
                else discord.ButtonStyle.secondary
            ),
            disabled=disabled,
            row=2,
        )

        async def change_page(interaction: discord.Interaction):
            self.selected.clear()
            next_panel = DistributePanel(
                self.requests,
                self.snapshot,
                cap=self.cap,
                today=self.today,
                page=page,
            )
            next_panel.message = interaction.message
            await interaction.response.edit_message(
                embed=next_panel.build_embed(), view=next_panel
            )

        button.callback = change_page
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The private channel is the authorization gate.

        Discord already stops anyone who cannot see the channel from
        clicking. This re-checks against the CURRENTLY recorded officer
        channel -- not the one captured when the panel was built -- so
        that moving the officer channel with !setofficerchannel
        immediately makes panels left behind in the old channel inert.
        """
        if not is_officer_channel(interaction.channel_id):
            await interaction.response.send_message(
                "This panel only works in the current officers' channel.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        """Say the panel expired instead of failing silently.

        Once the timeout elapses discord.py stops dispatching component
        interactions, so without this an officer clicking an old panel
        gets Discord's generic "interaction failed" and no explanation.
        The queue itself is untouched -- only this view is dead.
        """
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏳ This panel expired. Run `!distribute` for a fresh one.",
                view=self,
            )
        except discord.HTTPException:
            pass

    async def _on_pick(self, interaction: discord.Interaction):
        self.selected[interaction.user.id] = self.picker.values[0]
        await interaction.response.defer()

    async def _require_selection(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.selected:
            await interaction.response.send_message(
                "Pick a request from the dropdown first.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Approve", style=discord.ButtonStyle.success, emoji="✅", row=1
    )
    async def approve_button(self, interaction: discord.Interaction, _button):
        if not await self._require_selection(interaction):
            return
        await interaction.response.defer()
        result = await approve(self.selected[interaction.user.id], interaction.user.display_name)
        self.selected.pop(interaction.user.id, None)
        await interaction.followup.send(result)
        await refresh_panel(interaction, self.page)

    @discord.ui.button(
        label="Deny", style=discord.ButtonStyle.danger, emoji="❌", row=1
    )
    async def deny_button(self, interaction: discord.Interaction, _button):
        if not await self._require_selection(interaction):
            return
        await interaction.response.defer()
        result = await deny(self.selected[interaction.user.id])
        self.selected.pop(interaction.user.id, None)
        await interaction.followup.send(result)
        await refresh_panel(interaction, self.page)


async def refresh_panel(interaction: discord.Interaction, page: int) -> None:
    """Redraw one page of the queue in place after a request resolves.

    `page` is the page this panel is showing, so page 2 stays page 2
    rather than being replaced by page 1's contents. Other pages go
    stale, which is harmless: approve() and deny() both re-check the
    queue, so a click on a stale entry reports that it was already
    handled rather than acting twice.
    """
    try:
        snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
    except Exception:
        return
    requests = list(_STATE.queue)
    page = min(page, page_count(requests) - 1)
    view = (
        DistributePanel(
            requests, snapshot, cap=gear_cap(), today=today_pht(), page=page
        )
        if requests
        else None
    )
    if view is None:
        embed = build_panel_embed([], snapshot, gear_cap(), today_pht())
    else:
        embed = view.build_embed()
    await interaction.message.edit(embed=embed, view=view)
    if view is not None:
        view.message = interaction.message


@bot.command(name="distribute")
async def distribute_cmd(ctx):
    """Show the pending requests with approve/deny controls."""
    if not is_officer_channel(ctx.channel.id):
        return  # silently ignored outside the officers' channel

    try:
        snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
    except Exception as exc:
        await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
        return

    requests = list(_STATE.queue)
    view = (
        DistributePanel(requests, snapshot, cap=gear_cap(), today=today_pht())
        if requests
        else None
    )
    embed = (
        view.build_embed()
        if view is not None
        else build_panel_embed([], snapshot, gear_cap(), today_pht())
    )
    message = await ctx.send(embed=embed, view=view)
    if view is not None:
        view.message = message


def requests_for_user(state: items_state.State, user_id: int) -> list[items_state.PendingRequest]:
    return [r for r in state.queue if r.user_id == user_id]


def drop_special_requests(state: items_state.State) -> list[items_state.PendingRequest]:
    """Remove every queued special log request, returning them.

    Special logs are raffled now. A request queued under the old rules
    can no longer be approved into a sensible outcome, and leaving it in
    the queue would show members a board line that never resolves.
    """
    dropped = [r for r in state.queue if r.type == items_rules.SPECIAL]
    for request in dropped:
        state.queue.remove(request)
    return dropped


async def announce_dropped_specials(channel) -> None:
    """Drop stranded special requests and tell the officers who was waiting.

    Called from BOTH restore paths in on_ready. The pin-scanning path
    recovers a queue just as completely as the configured-channel one, so
    skipping it there would leave the stranded requests alive on exactly
    the redeploy that has no officer channel configured yet.
    """
    dropped = drop_special_requests(_STATE)
    if not dropped:
        return

    await save_state(channel)
    await refresh_board()
    lines = "\n".join(
        f"• **{r.item}** for **{r.ign}** (<@{r.user_id}>)" for r in dropped
    )
    await channel.send(
        embed=error_embed(
            "Special log requests removed",
            "Special logs are raffled now, so these queued requests "
            f"were dropped:\n{lines}\n\nTell these members to answer "
            "the poll in the raffle channel instead.",
        )
    )


def cancellable(
    state: items_state.State, user_id: int, item_query: str
) -> tuple[items_state.PendingRequest | None, str | None]:
    """Which of this member's pending requests !cancelrequest means.

    Returns (request, error_message); exactly one is None.
    """
    mine = requests_for_user(state, user_id)
    if not mine:
        return None, "You have no pending requests."

    query = item_query.strip()
    if not query:
        if len(mine) == 1:
            return mine[0], None
        names = ", ".join(f"`{r.item}`" for r in mine)
        return None, f"You have several pending: {names}. Say which: `!cancelrequest <item name>`"

    wanted = items_rules.normalize(query)
    for request in mine:
        if items_rules.normalize(request.item) == wanted:
            return request, None
    return None, f"You have no pending request for {query!r}."


@bot.command(name="cancelrequest")
async def cancelrequest_cmd(ctx, *, item_query: str = ""):
    """Withdraw your own pending request."""
    async with _SHEET_LOCK:
        request, error = cancellable(_STATE, ctx.author.id, item_query)
        if error is not None:
            await ctx.send(embed=error_embed("Nothing cancelled", error))
            return
        items_state.remove_request(_STATE, request.id)
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)
        await refresh_board()
    await ctx.send(
        embed=ok_embed("Request cancelled", f"Withdrew **{request.item}** for **{request.ign}**.")
    )


@bot.command(name="myrequests")
async def myrequests_cmd(ctx):
    """List your pending requests."""
    mine = requests_for_user(_STATE, ctx.author.id)
    if not mine:
        await ctx.send(embed=ok_embed("Nothing pending", "You have no pending requests."))
        return
    body = "\n".join(f"• **{r.item}** for **{r.ign}** — requested {r.requested_at}" for r in mine)
    await ctx.send(embed=_embed("📋 Your Pending Requests", body, 0x3498DB))


@bot.command(name="itemhelp")
async def itemhelp_cmd(ctx):
    """Explain the commands and the rules."""
    embed = _embed(
        "📦 Item Requests",
        "**`!request <item name> <IGN>`** — ask for a **gear log**. "
        "Example: `!request Asta's Belt Kobe`\n"
        "**`!myrequests`** — see what you have pending\n"
        "**`!cancelrequest [item name]`** — withdraw a request\n\n"
        "**Rules**\n"
        f"• Gear logs: {gear_cap()} per player per day, resetting at "
        "midnight (Manila time).\n"
        "• Special logs cannot be requested — they are raffled.\n\n"
        "Your IGN must match your row in the Logs Tracker sheet.",
        0x3498DB,
    )
    embed.add_field(
        name="🎲 Special log raffle",
        value=(
            "Special logs are drawn from a poll, not a queue. Answer **Yes** "
            "on the poll in the raffle channel to enter. A log you already "
            "have is skipped automatically.\n\n"
            "_Raffle roles only:_\n"
            "**`!poll <special log> [--hours N]`** — open a poll "
            f"({items_raffle.DEFAULT_POLL_HOURS}h by default)\n"
            "**`!list <special log>`** — after it closes, who is eligible\n"
            "**`!winner <special log> <IGN>`** — record the draw\n"
            "**`!cancelpoll <special log>`** — cancel an open poll"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Admins",
        value=(
            "**`!setofficerchannel`** — run first; the bot stores its queue "
            "here\n"
            "**`!setqueuechannel`** — pin a public queue board\n"
            "**`!setraffleroles @role [@role ...]`** — who may run the raffle\n"
            "**`!setrafflechannel`** — where polls are posted"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.event
async def on_ready():
    print(f"[items] logged in as {bot.user}", flush=True)
    if _STATE.officer_channel_id is None:
        # Nothing to restore from until an admin has named the channel.
        # Scan every readable text channel's pins once, so a redeploy
        # recovers without anyone re-running !setofficerchannel.
        for guild in bot.guilds:
            for channel in guild.text_channels:
                try:
                    if await load_state(channel):
                        print(f"[items] restored state from #{channel.name}", flush=True)
                        await announce_dropped_specials(channel)
                        return
                except discord.HTTPException:
                    continue
        print("[items] no state found; run !setofficerchannel", flush=True)
        return

    channel = bot.get_channel(_STATE.officer_channel_id)
    if channel is not None:
        await load_state(channel)
        await announce_dropped_specials(channel)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, channel_guard.WrongChannel):
        # Silent on purpose: a reply would post into the very channel the
        # guard is keeping quiet, and would advertise that the command exists.
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=error_embed("Not allowed", "That command is for administrators."))
        return
    print(f"[items] command error: {error!r}", flush=True)
    await ctx.send(embed=error_embed("Something went wrong", str(error)))


def main() -> None:
    global _SPREADSHEET
    missing = missing_credentials(os.environ)
    if missing:
        print(
            f"[items] not configured, missing: {', '.join(missing)}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_NOT_CONFIGURED)

    _SPREADSHEET = items_sheet.open_logs_tracker(
        os.environ["ITEMS_SHEET_ID"], os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    )
    bot.run(os.environ["ITEMS_DISCORD_TOKEN"])



POLL_ANSWER = "Yes"


def build_poll(item: str, hours: int) -> discord.Poll:
    """A single-answer poll: voting is entering, so there is nothing to read."""
    poll = discord.Poll(
        question=item, duration=datetime.timedelta(hours=hours)
    )
    poll.add_answer(text=POLL_ANSWER)
    return poll


@bot.command(name="poll")
async def poll_cmd(ctx, *, argument: str = ""):
    """Open a raffle for one special log."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    try:
        parsed = items_raffle.parse_poll_argument(argument)
    except items_raffle.RaffleArgumentError as exc:
        await ctx.send(embed=error_embed("Poll refused", str(exc)))
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        try:
            item = items_rules.resolve_special(
                parsed.item_query, snapshot.special_headers, snapshot.gear_headers
            )
        except items_rules.ItemLookupError as exc:
            await ctx.send(embed=error_embed("Poll refused", str(exc)))
            return

        now = items_rules.now_pht()
        now_text = items_rules.format_timestamp(now)
        existing = items_state.find_raffle(_STATE, item)
        if existing is not None and existing.ends_at > now_text and not existing.winners:
            await ctx.send(
                embed=error_embed(
                    "Poll refused",
                    f"A raffle for **{item}** is already open. It closes at "
                    f"{existing.ends_at} PHT.",
                )
            )
            return

        # An earlier raffle for this item that ended without a winner is
        # superseded, not kept. find_raffle only ever returns the newest
        # raffle for a name, so leaving the old one behind would make it
        # unreachable by !list and !winner AND unevictable (eviction takes
        # only drawn raffles) -- a slot leaked until someone edited the
        # pinned state by hand. A drawn raffle is real history and stays.
        superseded = existing if existing is not None and not existing.winners else None
        if superseded is not None:
            _STATE.raffles.remove(superseded)

        allowed, victim = items_state.raffle_to_evict(_STATE)
        if not allowed:
            if superseded is not None:
                _STATE.raffles.append(superseded)
            await ctx.send(
                embed=error_embed(
                    "Poll refused",
                    f"All {items_state.MAX_RAFFLES} tracked raffles are still "
                    "waiting for a winner. Run `!winner` on one of them "
                    "first — the bot will not discard a raffle you have not "
                    "drawn yet.",
                )
            )
            return

        try:
            message = await ctx.channel.send(poll=build_poll(item, parsed.hours))
        except Exception as exc:
            # Nothing has been given up yet: the victim is still in state
            # and the superseded raffle goes back, because no replacement
            # poll exists to supersede it.
            if superseded is not None:
                _STATE.raffles.append(superseded)
            await ctx.send(embed=error_embed("Could not post the poll", str(exc)))
            return

        # Paid for only now that Discord has accepted the poll.
        if victim is not None:
            _STATE.raffles.remove(victim)

        # Recorded only once Discord has confirmed the message, so a
        # failed post can never leave a raffle pointing at nothing.
        raffle = items_state.Raffle(
            item=item,
            channel_id=ctx.channel.id,
            message_id=message.id,
            created_at=now_text,
            ends_at=items_rules.format_timestamp(
                now + datetime.timedelta(hours=parsed.hours)
            ),
        )
        _STATE.raffles.append(raffle)

        if not items_state.fits(_STATE):
            # Put back everything the attempt spent. The victim was
            # removed to pay for a raffle that is not being kept, and
            # losing a drawn raffle's record is not an acceptable price
            # for a poll that was never recorded.
            _STATE.raffles.remove(raffle)
            if victim is not None:
                _STATE.raffles.append(victim)
            if superseded is not None:
                _STATE.raffles.append(superseded)
            _STATE.raffles.sort(key=lambda r: r.created_at)
            await ctx.send(
                embed=error_embed(
                    "Poll not recorded",
                    "The bot's storage is full, so this raffle could not be "
                    "saved. The poll above will not be tracked -- delete it, "
                    "clear the request queue, and try again.",
                )
            )
            return

        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

    note = ""
    if superseded is not None:
        note = (
            f"\n\n⚠️ This **replaces** the earlier raffle for **{item}** that "
            "closed without a winner. Its entry list is gone."
        )
    await ctx.send(
        embed=ok_embed(
            "Raffle open",
            f"**{item}** — answer **{POLL_ANSWER}** above to enter. Closes at "
            f"{raffle.ends_at} PHT. Run `!list {item}` after that.{note}",
        )
    )


def poll_is_open(poll) -> bool:
    """Whether Discord itself still considers this poll open.

    The stored ends_at is computed before the poll is posted, so it runs
    a little ahead of the expiry Discord actually assigned. In that gap
    !list would read a partial voter list and freeze it permanently.
    Discord's own expiry settles it; when the object cannot answer (an
    older poll payload), False defers to the stored timestamp, which the
    caller has already checked.
    """
    if poll is None:
        return False
    expires_at = getattr(poll, "expires_at", None)
    if expires_at is not None:
        try:
            return expires_at > discord.utils.utcnow()
        except TypeError:
            return False
    is_finalised = getattr(poll, "is_finalised", None)
    return not is_finalised() if callable(is_finalised) else False


async def poll_voters(message) -> list[items_raffle.Voter]:
    """Everyone who answered Yes, as (id, nickname) pairs.

    The answer is found by its text rather than by index: an id is only
    stable for a poll this bot created, and a raffle recorded before a
    restart must still be readable.

    A voter arrives as a Member when the members intent is on and the
    guild is cached, and as a User otherwise. Only the Member carries the
    server nickname, so a User is looked up once over HTTP before giving
    up on the global name.
    """
    poll = getattr(message, "poll", None)
    if poll is None or not poll.answers:
        raise LookupError("that message no longer carries a poll")

    answer = next(
        (a for a in poll.answers if a.text.strip().casefold() == POLL_ANSWER.casefold()),
        poll.answers[0],
    )

    guild = getattr(message, "guild", None)
    voters: list[items_raffle.Voter] = []
    async for voter in answer.voters():
        display_name = getattr(voter, "display_name", "")
        if guild is not None and not isinstance(voter, discord.Member):
            try:
                member = await guild.fetch_member(voter.id)
            except Exception:
                member = None
            if member is not None:
                display_name = member.display_name
        voters.append(
            items_raffle.Voter(user_id=voter.id, display_name=display_name)
        )
    return voters


# Discord rejects an embed description longer than this, and !list saves
# the frozen pool before it sends -- so an over-long render would lose the
# officer's only view of a pool that has already been committed.
EMBED_DESCRIPTION_LIMIT = 4096


def _capped(names: list[str], budget: int, join: str) -> str:
    """As many names as fit, then a count of what was left out."""
    kept: list[str] = []
    used = 0
    for index, name in enumerate(names):
        addition = len(name) + (len(join) if kept else 0)
        remaining = len(names) - index
        tail = f"{join}…and {remaining} more"
        if used + addition + len(tail) > budget:
            return join.join(kept) + tail if kept else tail.lstrip(join)
        kept.append(name)
        used += addition
    return join.join(kept)


def _winner_footer(winners: tuple[str, ...]) -> str:
    if not winners:
        return ""
    label = "Winner" if len(winners) == 1 else "Winners"
    return f"🏆 **{label}: {', '.join(winners)}**"


def render_pool(
    item: str, split: items_raffle.VoterSplit, winners: tuple[str, ...] = ()
) -> str:
    """The three groups an officer needs, in one description.

    Bounded, because the eligible list is frozen and saved BEFORE this is
    sent: a description Discord refuses would leave the pool committed
    with nothing shown, and a retry replays the frozen list without the
    excluded and unidentified groups that only exist on the first run.
    The eligible list is the one that must survive truncation intact, so
    it gets the budget first.
    """
    header = f"**Eligible for {item}** ({len(split.eligible)})"
    trophy = _winner_footer(winners)
    footer = f"\n\n{trophy}" if trophy else ""
    budget = EMBED_DESCRIPTION_LIMIT - len(header) - len(footer) - 200

    numbered = [f"{n}. {ign}" for n, ign in enumerate(split.eligible, start=1)]
    lines = [header, _capped(numbered, budget, "\n") or "_nobody_"]
    budget -= len(lines[1])

    if split.already_have:
        block = "**Already has it** (excluded)"
        lines += ["", block, _capped(split.already_have, max(budget, 0), ", ")]
        budget -= len(lines[-1]) + len(block)
    if split.unidentified:
        block = "**Couldn't identify** — sort these out by hand"
        mentions = [f"<@{voter.user_id}>" for voter in split.unidentified]
        lines += ["", block, _capped(mentions, max(budget, 0), " ")]
    if trophy:
        lines += ["", trophy]
    return "\n".join(lines)


@bot.command(name="list")
async def list_cmd(ctx, *, argument: str = ""):
    """Show who is eligible for a closed raffle."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    item_query = argument.strip()
    raffle = items_state.find_raffle(_STATE, item_query) if item_query else None
    if raffle is None:
        await ctx.send(
            embed=error_embed(
                "Nothing to list",
                f"No raffle for {item_query!r}. Usage: `!list <special log name>`",
            )
        )
        return

    if raffle.listed:
        # Replayed verbatim, never recomputed: the pool a winner is drawn
        # from must not be able to change between looking and drawing.
        split = items_raffle.VoterSplit(eligible=list(raffle.eligible))
        await ctx.send(
            embed=ok_embed(
                f"Raffle: {raffle.item}", render_pool(raffle.item, split, raffle.winners)
            )
        )
        return

    now = items_rules.format_timestamp(items_rules.now_pht())
    if raffle.ends_at > now:
        await ctx.send(
            embed=error_embed(
                "Poll still open",
                f"**{raffle.item}** closes at {raffle.ends_at} PHT. "
                "Drawing before then would leave out anyone who has not voted.",
            )
        )
        return

    try:
        # The raffle's own channel, not wherever the command was typed.
        # An admin who moves the raffle channel mid-poll must still be
        # able to draw the poll that is sitting in the old one.
        source = bot.get_channel(raffle.channel_id) or ctx.channel
        message = await source.fetch_message(raffle.message_id)
        if poll_is_open(getattr(message, "poll", None)):
            await ctx.send(
                embed=error_embed(
                    "Poll still open",
                    f"Discord still has voting open on **{raffle.item}**. "
                    "Try again in a moment — freezing now would leave out "
                    "anyone who has not voted yet.",
                )
            )
            return
        voters = await poll_voters(message)
    except Exception as exc:
        await ctx.send(
            embed=error_embed(
                "Cannot read the poll",
                f"The poll message for **{raffle.item}** could not be read "
                f"({exc}). Run `!poll {raffle.item}` again to hold a new one.",
            )
        )
        return

    async with _SHEET_LOCK:
        # Re-resolved under the lock. The raffle above was found before
        # the poll fetch awaited, so a second officer running !list at the
        # same time reaches here holding a Raffle that has already been
        # swapped out of state -- replace_raffle would raise on it.
        raffle = items_state.find_raffle(_STATE, item_query)
        if raffle is None or raffle.listed:
            await ctx.send(
                embed=(
                    ok_embed(
                        f"Raffle: {raffle.item}",
                        render_pool(
                            raffle.item,
                            items_raffle.VoterSplit(eligible=list(raffle.eligible)),
                            raffle.winners,
                        ),
                    )
                    if raffle is not None
                    else error_embed(
                        "Nothing to list", f"No raffle for {item_query!r}."
                    )
                )
            )
            return

        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        split = items_raffle.classify_voters(
            voters,
            snapshot.roster,
            holds=lambda ign: items_sheet.holds_special(snapshot, ign, raffle.item),
        )
        updated = items_state.replace_raffle(
            _STATE, raffle, eligible=tuple(split.eligible), listed=True
        )
        # A pool too big for one pinned message would make save_state give
        # up -- not just now, but on every later save, silently halting
        # queue persistence. Refuse the freeze instead, the same way
        # !request and !poll refuse a state they could not store.
        if not items_state.fits(_STATE):
            items_state.replace_raffle(
                _STATE, updated, eligible=raffle.eligible, listed=raffle.listed
            )
            await ctx.send(
                embed=error_embed(
                    "Entry list too large",
                    f"**{raffle.item}** drew {len(split.eligible)} eligible "
                    "players — too large for the bot to store safely, so "
                    "nothing was frozen. Work the request queue down and try "
                    "again; if it still refuses, the raffle needs to be split.",
                )
            )
            return
        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

    await ctx.send(
        embed=ok_embed(
            f"Raffle: {updated.item}", render_pool(updated.item, split, updated.winners)
        )
    )


@bot.command(name="winner")
async def winner_cmd(ctx, *, argument: str = ""):
    """Record the winner of a closed raffle."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        try:
            item, igns = items_raffle.split_item_and_igns(
                argument, items_state.raffle_item_names(_STATE), snapshot.roster
            )
        except items_raffle.RaffleArgumentError as exc:
            await ctx.send(embed=error_embed("Winner refused", str(exc)))
            return

        raffle = items_state.find_raffle(_STATE, item)
        now = items_rules.format_timestamp(items_rules.now_pht())

        if raffle.drawn:
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"**{raffle.item}** has already been drawn: "
                    f"**{', '.join(raffle.winners)}** won it.",
                )
            )
            return
        if raffle.ends_at > now:
            await ctx.send(
                embed=error_embed(
                    "Poll still open",
                    f"**{raffle.item}** closes at {raffle.ends_at} PHT.",
                )
            )
            return
        if not raffle.listed:
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"Run `!list {raffle.item}` first. The winner is checked "
                    "against the eligible list that command freezes.",
                )
            )
            return

        # Every name is checked before the first write, so a typo in the
        # third name cannot leave the first two ticked.
        recorded_already: list[str] = []
        missing: list[str] = []
        chosen: list[str] = []
        for ign in igns:
            wanted = items_rules.normalize(ign)
            if any(items_rules.normalize(w) == wanted for w in raffle.winners):
                recorded_already.append(ign)
                continue
            on_list = next(
                (n for n in raffle.eligible if items_rules.normalize(n) == wanted),
                None,
            )
            (chosen if on_list is not None else missing).append(on_list or ign)

        if missing:
            suggestions = get_close_matches(
                missing[0], list(raffle.eligible), n=3, cutoff=0.6
            )
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"{', '.join(missing)} — not on the eligible list for "
                    f"**{raffle.item}**.{hint} Nothing was recorded.",
                )
            )
            return
        if recorded_already:
            await ctx.send(
                embed=error_embed(
                    "Winner refused",
                    f"**{', '.join(recorded_already)}** already won "
                    f"**{raffle.item}** — nothing was written a second time. "
                    f"Re-run naming only the players still to record.",
                )
            )
            return

        written: list[str] = []
        already_ticked: list[str] = []
        ledger_gaps: list[tuple[str, str, list[str]]] = []
        failure: tuple[str, str] | None = None
        not_attempted: list[str] = []

        for position, ign in enumerate(chosen):
            try:
                # ign is bound as a default: a bare closure would send
                # the LAST name of the loop to every thread.
                await asyncio.to_thread(
                    lambda ign=ign: items_sheet.commit_approval(
                        _SPREADSHEET,
                        ign=ign,
                        item=raffle.item,
                        item_type=items_rules.SPECIAL,
                        timestamp=now,
                        officer=getattr(ctx.author, "display_name", str(ctx.author)),
                        user_id=ctx.author.id,
                        request_id=items_state.new_request_id(),
                    )
                )
            except items_sheet.LedgerWriteError as exc:
                # The checkbox IS ticked, so this name is recorded and a
                # retry would skip it -- the ledger row has to be handed
                # over now or it is lost.
                written.append(ign)
                ledger_gaps.append((ign, exc.address, exc.row))
            except items_sheet.AlreadyHeld:
                # A previous run wrote the sheet and then failed to save
                # state. The item HAS been given; say so and move on.
                written.append(ign)
                already_ticked.append(ign)
            except Exception as exc:
                failure = (ign, str(exc))
                not_attempted = chosen[position + 1 :]
                break
            else:
                written.append(ign)

        updated = items_state.replace_raffle(
            _STATE,
            raffle,
            winners=(*raffle.winners, *written),
            drawn=failure is None,
        )
        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

    lines: list[str] = []
    if written:
        label = "Winner" if len(written) == 1 else "Winners"
        lines.append(
            f"🏆 **{label}: {', '.join(written)}** — ticked in "
            f"`{items_sheet.SPECIAL_TAB}`."
        )
    if already_ticked:
        lines.append(
            f"⚠️ {', '.join(already_ticked)} was already ticked in the sheet, "
            "so nothing was written a second time."
        )
    for ign, address, row in ledger_gaps:
        pasteable = " | ".join(row)
        lines.append(
            f"⚠️ {ign}'s cell {address} is ticked but the "
            f"`{items_sheet.LEDGER_TAB}` row failed. Do NOT re-run for {ign} — "
            f"add this row by hand:\n```\n{pasteable}\n```"
        )
    if failure is not None:
        ign, reason = failure
        remaining = [ign, *not_attempted]
        lines.append(f"❌ **{ign}** was not recorded: {reason}")
        if not_attempted:
            lines.append(f"⏸️ Not attempted: {', '.join(not_attempted)}")
        lines.append(
            f"The raffle is still open. Re-run:\n"
            f"`!winner {updated.item} {' - '.join(remaining)}`"
        )
    else:
        lines.append(
            "They are no longer eligible for this log. The raffle is closed."
        )

    title = "Winners recorded" if len(written) != 1 else "Winner recorded"
    await ctx.send(
        embed=(
            error_embed("Partly recorded", "\n\n".join(lines))
            if failure is not None
            else ok_embed(title, "\n\n".join(lines))
        )
    )


@bot.command(name="cancelpoll")
async def cancelpoll_cmd(ctx, *, argument: str = ""):
    """Cancel an open raffle poll."""
    if await _refuse_raffle(ctx, raffle_access(ctx)):
        return

    async with _SHEET_LOCK:
        item_query = argument.strip()
        if not item_query:
            await ctx.send(
                embed=error_embed(
                    "Nothing cancelled", "Usage: `!cancelpoll <special log name>`"
                )
            )
            return

        raffle = items_state.find_raffle(_STATE, item_query)
        if raffle is None:
            names = ", ".join(
                f"`{item}`" for item in items_state.raffle_item_names(_STATE)
            ) or "_none_"
            await ctx.send(
                embed=error_embed(
                    "Nothing cancelled",
                    f"No tracked raffle for {item_query!r}. Tracked raffles: {names}.",
                )
            )
            return

        if raffle.winners:
            await ctx.send(
                embed=error_embed(
                    "Cancel refused",
                    f"**{raffle.item}** has already been drawn: "
                    f"**{', '.join(raffle.winners)}** won it. A drawn raffle "
                    "is distribution history.",
                )
            )
            return

        now = items_rules.format_timestamp(items_rules.now_pht())
        if raffle.ends_at <= now:
            await ctx.send(
                embed=error_embed(
                    "Cancel refused",
                    f"**{raffle.item}** closed at {raffle.ends_at} PHT. "
                    f"Run `!poll {raffle.item}` to supersede it.",
                )
            )
            return

        try:
            # The raffle stays with the channel where its poll was posted,
            # even when an admin moves the configured raffle channel mid-poll.
            source = bot.get_channel(raffle.channel_id) or ctx.channel
            message = await source.fetch_message(raffle.message_id)
        except discord.NotFound:
            message = None
        except Exception as exc:
            await ctx.send(
                embed=error_embed(
                    "Could not cancel the poll",
                    f"The poll message for **{raffle.item}** could not be read "
                    f"({exc}). Nothing was cancelled.",
                )
            )
            return

        delete_failed = False
        if message is not None:
            if poll_is_open(getattr(message, "poll", None)):
                try:
                    await message.end_poll()
                except Exception as exc:
                    await ctx.send(
                        embed=error_embed(
                            "Could not cancel the poll",
                            f"The poll for **{raffle.item}** could not be ended "
                            f"({exc}). Nothing was cancelled.",
                        )
                    )
                    return

            try:
                await message.delete()
            except Exception:
                delete_failed = True

        _STATE.raffles.remove(raffle)
        channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if channel is not None:
            await save_state(channel)

        if message is None:
            await ctx.send(
                embed=warn_embed(
                    "Poll cancelled, message missing",
                    f"The tracked poll message for **{raffle.item}** was already "
                    "gone, so its raffle record was removed. If an old poll "
                    "message is still visible, delete it by hand.",
                )
            )
            return

        if delete_failed:
            await ctx.send(
                embed=warn_embed(
                    "Poll cancelled, message left behind",
                    f"**{raffle.item}** was ended and its raffle record was "
                    "removed, but Discord could not delete the poll message. "
                    "Delete that message by hand.",
                )
            )
            return

        await ctx.send(
            embed=ok_embed(
                "Poll cancelled",
                f"**{raffle.item}** was ended and removed. Run `!poll "
                f"{raffle.item}` to open a new poll.",
            )
        )


# Must stay the LAST statement in this file. bot.run() blocks, so any
# @bot.command defined below this point would never be registered -- and
# the tests would not notice, because importing the module skips main().
if __name__ == "__main__":
    main()
