"""Discord bot for guild item requests and officer distribution.

A third process alongside the timer and the attendance bot, with its own
Discord token and its own spreadsheet, so nothing it does can affect
either of them. See supervisor.py for why all three share one Render
service.

Authorization is the private officer channel itself: !distribute is
accepted only there, and a button attached to a message in that channel
can only be pressed by someone Discord already lets see the channel.
There is no role configuration to drift.
"""

import asyncio
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

import items_rules
import items_sheet
import items_state

load_dotenv()

EXIT_NOT_CONFIGURED = 78

REQUIRED_ENV = ("ITEMS_DISCORD_TOKEN", "ITEMS_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")

# How long a !distribute panel accepts clicks. The queue outlives the
# panel; expiry only means the officer re-runs the command.
PANEL_TIMEOUT = 900  # 15 minutes

# Serializes every read-then-write pair. Two officers approving at once
# would otherwise both read "2 used today" and both write, yielding 4.
_SHEET_LOCK = asyncio.Lock()

_STATE = items_state.State()

# The pinned message holding _STATE, cached so save_state edits it
# instead of posting a new one on every change.
_STATE_MESSAGE: discord.Message | None = None

_SPREADSHEET = None

intents = discord.Intents.default()
intents.message_content = True
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


async def save_state(channel) -> list[items_state.PendingRequest]:
    """Write _STATE into the pinned message. Returns anything dropped.

    Anything the encoder had to drop to fit is removed from _STATE.queue
    too. Without that, the dropped requests survive in memory, still
    appear in panels, and force the encoder to drop them again on every
    single save -- while a restart resurrects a state message that never
    contained them. Memory and storage must agree.
    """
    global _STATE_MESSAGE
    content, dropped = items_state.encode_state(_STATE)
    if dropped:
        gone = {r.id for r in dropped}
        _STATE.queue = [r for r in _STATE.queue if r.id not in gone]

    if _STATE_MESSAGE is not None:
        try:
            await _STATE_MESSAGE.edit(content=content)
            return dropped
        except discord.HTTPException:
            _STATE_MESSAGE = None

    _STATE_MESSAGE = await channel.send(content)
    try:
        await _STATE_MESSAGE.pin()
    except discord.HTTPException:
        # Pinning needs Manage Messages. Without it the message still
        # works -- load_state scans history too -- so this is not fatal.
        pass
    return dropped


async def load_state(channel) -> bool:
    """Restore _STATE from the channel's pinned messages.

    Returns True if a state message was found.

    channel.pins() is an ASYNC ITERATOR in discord.py 2.7, not a
    coroutine returning a list -- `await channel.pins()` raises
    TypeError. It must be consumed with `async for`.
    """
    global _STATE_MESSAGE
    candidates = [message async for message in channel.pins(limit=50)]
    for message in candidates:
        decoded = items_state.decode_state(message.content)
        if decoded is None:
            continue
        _STATE.officer_channel_id = decoded.officer_channel_id or channel.id
        _STATE.queue = decoded.queue
        _STATE.igns = decoded.igns
        _STATE_MESSAGE = message
        return True
    return False


def _embed(title: str, description: str, colour: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=colour)


def ok_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"✅ {title}", description, 0x2ECC71)


def error_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"❌ {title}", description, 0xE74C3C)


def is_officer_channel(channel_id: int) -> bool:
    return (
        _STATE.officer_channel_id is not None
        and channel_id == _STATE.officer_channel_id
    )


@bot.command(name="setofficerchannel")
@commands.has_permissions(administrator=True)
async def setofficerchannel_cmd(ctx):
    """Record this channel as the officers' channel."""
    _STATE.officer_channel_id = ctx.channel.id
    await save_state(ctx.channel)
    await ctx.send(
        embed=ok_embed(
            "Officer channel set",
            f"`!distribute` now works in {ctx.channel.mention}, and the bot "
            "keeps its request queue in a pinned message here. Don't delete it.",
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
        pending_gear=items_state.pending_gear_for(state, parsed.ign),
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

        _STATE.queue.append(outcome.request)
        _STATE.igns[str(ctx.author.id)] = outcome.request.ign

        channel = bot.get_channel(_STATE.officer_channel_id)
        dropped = await save_state(channel) if channel else []

    await ctx.send(embed=ok_embed("Request queued", outcome.message))
    if dropped and channel:
        names = ", ".join(f"{d.item} ({d.ign})" for d in dropped)
        await channel.send(
            embed=error_embed(
                "Queue overflowed",
                f"The queue no longer fits in one message. These oldest "
                f"requests were dropped and must be re-requested: {names}",
            )
        )
