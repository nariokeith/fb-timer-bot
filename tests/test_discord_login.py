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


# The Cloudflare block page names the IP it is blocking. That one line is
# what tells a redeploy that landed on a fresh egress IP apart from one
# that came back to the same banned address -- worth keeping when the
# other 200 lines of the page are not.
CLOUDFLARE_PAGE_WITH_IP = (
    '<!doctype html><html><body>'
    '<span class="cf-footer-item sm:block sm:mb-1">'
    '<span>Cloudflare Ray ID: <strong>a2ccda67aa69afe8</strong></span></span>'
    '<span id="cf-footer-item-ip" class="cf-footer-item hidden sm:block sm:mb-1">'
    'Your IP: <button type="button" id="cf-footer-ip-reveal">Click to reveal</button>'
    '<span class="hidden" id="cf-footer-ip">74.220.48.29</span>'
    '</span></body></html>'
)


def test_the_blocked_ip_is_reported():
    bot = _Bot(_http_exception(429, CLOUDFLARE_PAGE_WITH_IP))

    with pytest.raises(SystemExit):
        discord_login.run(bot, "token")


def test_the_blocked_ip_appears_in_the_message(capsys):
    bot = _Bot(_http_exception(429, CLOUDFLARE_PAGE_WITH_IP))

    with pytest.raises(SystemExit):
        discord_login.run(bot, "token")

    err = capsys.readouterr().err
    assert "74.220.48.29" in err
    assert "<span" not in err, "the page itself must still be suppressed"


def test_a_page_without_an_ip_still_reports_the_ban(capsys):
    bot = _Bot(_http_exception(429, CLOUDFLARE_BODY))

    with pytest.raises(SystemExit):
        discord_login.run(bot, "token")

    err = capsys.readouterr().err
    assert "rate-limited" in err.lower()


def test_blocked_ip_of_a_page_without_one_is_none():
    assert discord_login.blocked_ip(CLOUDFLARE_BODY) is None


def test_blocked_ip_reads_the_cloudflare_footer():
    assert discord_login.blocked_ip(CLOUDFLARE_PAGE_WITH_IP) == "74.220.48.29"


# -- Two different 429s, two different waits ---------------------------------
#
# Cloudflare's 1015 is an edge ban on the IP: it lifts only after the
# address goes quiet, so waiting half an hour is right. Discord's own
# "exceeding global rate limits" is a far shorter application-layer block
# -- observed 2026-08-18 lasting about nine minutes -- and arrives as JSON
# rather than an HTML page. Treating it like a 1015 kept every bot down
# for thirty minutes over something that had already cleared.

DISCORD_GLOBAL_BODY = (
    "You are being blocked from accessing our API temporarily due to "
    "exceeding global rate limits. Refer to "
    "https://discord.com/developers/docs/topics/rate-limits for more information."
)


def test_a_cloudflare_page_is_an_edge_ban():
    assert discord_login.is_edge_ban(CLOUDFLARE_PAGE_WITH_IP)
    assert discord_login.is_edge_ban(CLOUDFLARE_BODY)


def test_discords_global_limit_is_not_an_edge_ban():
    assert not discord_login.is_edge_ban(DISCORD_GLOBAL_BODY)


def test_an_edge_ban_exits_with_the_long_cooldown_code():
    bot = _Bot(_http_exception(429, CLOUDFLARE_PAGE_WITH_IP))

    with pytest.raises(SystemExit) as exc_info:
        discord_login.run(bot, "token")

    assert exc_info.value.code == discord_login.EXIT_RATE_LIMITED


def test_a_global_limit_exits_with_the_brief_cooldown_code():
    bot = _Bot(_http_exception(429, DISCORD_GLOBAL_BODY))

    with pytest.raises(SystemExit) as exc_info:
        discord_login.run(bot, "token")

    assert exc_info.value.code == discord_login.EXIT_RATE_LIMITED_BRIEF


def test_a_global_limit_is_not_reported_as_cloudflare(capsys):
    """The old message called every login 429 a Cloudflare 1015."""
    bot = _Bot(_http_exception(429, DISCORD_GLOBAL_BODY))

    with pytest.raises(SystemExit):
        discord_login.run(bot, "token")

    err = capsys.readouterr().err
    assert "1015" not in err and "Cloudflare" not in err
    assert "global" in err.lower()


def test_the_brief_code_matches_the_supervisors():
    import supervisor

    assert discord_login.EXIT_RATE_LIMITED_BRIEF == supervisor.EXIT_RATE_LIMITED_BRIEF
