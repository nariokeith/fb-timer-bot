"""Where each bot is willing to accept commands.

All three bots share the "!" prefix in one guild, so discord.py hands a
command to whichever bot has it registered, from any channel that bot can
read -- !request typed in the attendance channel really did run the items
bot's request flow. The !set*channel commands only ever recorded where a
bot *posts*; they never constrained where it listens. Each bot registers
one global check built from this module to close that gap.
"""

from discord.ext import commands

# Returned by a resolver for the commands that must work anywhere. The
# !set*channel commands are how a channel gets configured in the first
# place, so they cannot themselves require one -- gating them would make
# a fresh guild unbootstrappable and a mistyped channel unfixable.
EXEMPT = "\x00exempt"


class WrongChannel(commands.CheckFailure):
    """A command was typed outside the channel(s) it is configured for.

    Deliberately its own subclass rather than a bare CheckFailure: both
    attendance_bot and items_bot reply to CheckFailure in their error
    handlers, so a bare one would post "couldn't run that" into every
    channel this guard exists to keep quiet -- noisier than the leak it
    fixes. Handlers swallow this type alone, leaving every other
    CheckFailure (bad input, missing role) replying exactly as before.
    """


def allows(channel_id, allowed) -> bool:
    """True when a command may run in `channel_id`.

    `allowed` is EXEMPT, or an iterable that may contain None for
    settings never configured; those are dropped. When nothing is left
    the bot is unconfigured and the guard stays inert -- so deploying
    this before anyone runs the setup commands changes no behavior, and
    a bot whose saved state fails to load degrades to today's behavior
    instead of refusing every command.
    """
    if allowed is EXEMPT:
        return True
    configured = {cid for cid in allowed if cid is not None}
    if not configured:
        return True
    return channel_id in configured


def make_check(resolver):
    """Build the global check a bot registers with bot.add_check().

    `resolver(ctx)` returns EXEMPT, or the channel ids ctx.command may
    run in. Refusal raises rather than returning False so the bot's
    error handler can tell this apart from a command that merely
    declined, and so discord.py stops before the command body runs.
    """

    async def check(ctx) -> bool:
        # None for an unrecognised "!foo" -- three bots share the prefix,
        # so most messages reaching any one bot are another bot's command.
        if ctx.command is None:
            return True
        if allows(ctx.channel.id, resolver(ctx)):
            return True
        raise WrongChannel(f"{ctx.command.qualified_name} is not used in this channel")

    return check
