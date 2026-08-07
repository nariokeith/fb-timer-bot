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
