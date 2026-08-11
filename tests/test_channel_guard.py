"""The rule deciding whether a command may run in the channel it was typed in."""

import asyncio

import pytest
from discord.ext import commands

import channel_guard


class FakeCommand:
    def __init__(self, name):
        self.name = name
        self.qualified_name = name


class FakeCtx:
    def __init__(self, channel_id, command_name="somecmd"):
        self.channel = type("Channel", (), {"id": channel_id})()
        self.command = FakeCommand(command_name)


def test_unconfigured_allows_every_channel():
    # The whole safety property: a bot nobody has set up behaves as before.
    assert channel_guard.allows(123, (None, None)) is True
    assert channel_guard.allows(123, ()) is True


def test_matching_channel_is_allowed():
    assert channel_guard.allows(123, (123,)) is True


def test_other_channel_is_refused_once_configured():
    assert channel_guard.allows(999, (123,)) is False


def test_none_entries_are_ignored_but_real_ids_still_bind():
    # A bot with only some of its channels set still guards on the ones it has.
    assert channel_guard.allows(123, (None, 123)) is True
    assert channel_guard.allows(999, (None, 123)) is False


def test_exempt_allows_every_channel_even_when_configured():
    assert channel_guard.allows(999, channel_guard.EXEMPT) is True


def test_wrong_channel_is_a_check_failure_but_its_own_type():
    # Both bots' error handlers reply to CheckFailure. They must be able to
    # swallow this one specifically without silencing the others.
    assert issubclass(channel_guard.WrongChannel, commands.CheckFailure)
    assert channel_guard.WrongChannel is not commands.CheckFailure


def test_check_passes_in_an_allowed_channel():
    check = channel_guard.make_check(lambda ctx: (123,))
    assert asyncio.run(check(FakeCtx(123))) is True


def test_check_raises_wrong_channel_elsewhere():
    check = channel_guard.make_check(lambda ctx: (123,))
    with pytest.raises(channel_guard.WrongChannel):
        asyncio.run(check(FakeCtx(999)))


def test_check_ignores_a_message_that_is_not_a_command():
    # ctx.command is None for an unknown "!foo"; there is nothing to guard.
    ctx = FakeCtx(999)
    ctx.command = None
    check = channel_guard.make_check(lambda ctx: (123,))
    assert asyncio.run(check(ctx)) is True
