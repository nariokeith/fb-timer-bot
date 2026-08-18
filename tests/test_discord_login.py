"""The Cloudflare 1015 ban must become a distinct exit code, not a crash.

Discord's edge rate-limits by IP. When it trips, every login from this
instance gets a Cloudflare HTML error page instead of an API response,
discord.py raises HTTPException(429), and the bot dies with exit 1 --
indistinguishable, to the supervisor, from a bug. It restarts in seconds,
logs in again, and refreshes the very ban it is waiting out.
"""

import sys

import discord
import pytest

import discord_login


class _Response:
    """The bits of aiohttp.ClientResponse that HTTPException reads."""

    def __init__(self, status, reason):
        self.status = status
        self.reason = reason


CLOUDFLARE_BODY = (
    "<!doctype html><html><head><title>Access denied</title></head>"
    "<body>errorCode: 1015</body></html>"
)


def _http_exception(status, body):
    return discord.HTTPException(_Response(status, "Too Many Requests"), body)


class _Bot:
    """A stand-in for commands.Bot whose run() raises what we hand it."""

    def __init__(self, error=None):
        self.error = error
        self.token = None

    def run(self, token, **kwargs):
        self.token = token
        if self.error is not None:
            raise self.error


def test_a_cloudflare_ban_exits_with_the_rate_limited_code():
    bot = _Bot(_http_exception(429, CLOUDFLARE_BODY))

    with pytest.raises(SystemExit) as exc_info:
        discord_login.run(bot, "token")

    assert exc_info.value.code == discord_login.EXIT_RATE_LIMITED


def test_the_ban_is_reported_without_the_html_page(capsys):
    """The 200-line Cloudflare page buried the one line that mattered."""
    bot = _Bot(_http_exception(429, CLOUDFLARE_BODY))

    with pytest.raises(SystemExit):
        discord_login.run(bot, "token")

    err = capsys.readouterr().err
    assert "rate-limited" in err.lower()
    assert "<html" not in err and "doctype" not in err.lower()


def test_other_http_errors_are_not_swallowed():
    """Only the IP ban gets the special code; a 500 is still a crash."""
    error = _http_exception(500, "server error")
    bot = _Bot(error)

    with pytest.raises(discord.HTTPException):
        discord_login.run(bot, "token")


def test_a_bad_token_is_not_mistaken_for_a_ban():
    bot = _Bot(discord.LoginFailure("Improper token"))

    with pytest.raises(discord.LoginFailure):
        discord_login.run(bot, "token")


def test_a_clean_run_returns_normally():
    bot = _Bot()

    discord_login.run(bot, "token")

    assert bot.token == "token"


def test_the_exit_code_matches_the_supervisors():
    import supervisor

    assert discord_login.EXIT_RATE_LIMITED == supervisor.EXIT_RATE_LIMITED


ENTRY_POINTS = ("bot.py", "attendance_bot.py", "items_bot.py")


@pytest.mark.parametrize("filename", ENTRY_POINTS)
def test_every_bot_starts_through_discord_login(filename):
    """A bare bot.run() would crash-loop through a ban, silently.

    Checked at the source level because these calls live under
    `if __name__ == "__main__"`, which importing the module never runs --
    so a regression here is invisible to every other test.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / filename).read_text()

    assert "discord_login.run(bot," in source
    assert "\n    bot.run(" not in source, f"{filename} still calls bot.run() directly"
