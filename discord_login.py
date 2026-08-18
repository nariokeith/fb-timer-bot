"""Start a Discord bot, telling an IP ban apart from an ordinary crash.

Discord's edge (Cloudflare) rate-limits by source IP. When it trips, a
login gets a Cloudflare HTML error page -- error 1015 -- instead of an
API response, and discord.py raises HTTPException(429) out of run().

Left alone that surfaces as exit 1: the supervisor reads a plain crash,
restarts the bot within seconds, and the fresh login refreshes the very
ban it needs to wait out. Worse, all three bots share this instance's one
egress IP, so each one's restarts keep the other two locked out. The loop
sustains itself long after whatever burst first tripped the limit.

Exiting with EXIT_RATE_LIMITED instead lets the supervisor recognise the
condition and put *every* child on one service-wide cooldown, letting the
IP go quiet long enough for Cloudflare to lift the ban.
"""

import re
import sys

import discord

# Must match supervisor.EXIT_RATE_LIMITED. 75 is EX_TEMPFAIL from
# sysexits.h -- "temporary failure, the user is invited to retry".
EXIT_RATE_LIMITED = 75


def is_rate_limited(exc: BaseException) -> bool:
    """True if `exc` is Discord's edge turning us away by IP.

    A 429 that reaches this far is always the edge, never a per-route
    limit: discord.py handles ordinary 429s internally by sleeping out
    the retry-after, and only re-raises when the body is not JSON --
    i.e. when Cloudflare answered instead of Discord.
    """
    return isinstance(exc, discord.HTTPException) and exc.status == 429


# Cloudflare's block page hides the visitor's address in this span. It is
# the only part of those ~200 lines worth keeping: it says WHICH address
# is banned, which is how a redeploy that landed on a fresh egress IP is
# told apart from one that came back to the same blocked address.
_BLOCKED_IP = re.compile(r'id="cf-footer-ip"[^>]*>\s*([^<\s]+)')


def blocked_ip(page: str) -> str | None:
    """The IP Cloudflare says it is blocking, or None if the page lacks it."""
    match = _BLOCKED_IP.search(page or "")
    return match.group(1) if match else None


def run(bot, token: str, **kwargs) -> None:
    """bot.run(token), converting an IP ban into EXIT_RATE_LIMITED."""
    try:
        bot.run(token, **kwargs)
    except discord.HTTPException as exc:
        if not is_rate_limited(exc):
            raise
        # Deliberately not printing the exception: its text is the entire
        # Cloudflare error page, ~200 lines of HTML per attempt, which is
        # what buried this diagnosis in the deploy logs in the first place.
        # Only the blocked address is lifted back out of it.
        ip = blocked_ip(exc.text)
        where = f" blocking {ip}" if ip else ""
        print(
            f"Discord rate-limited this instance's IP{where} (HTTP 429, "
            "Cloudflare error 1015). Exiting so the supervisor can hold "
            "every bot off until the ban lifts.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_RATE_LIMITED)
