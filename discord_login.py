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
import time

import discord

# Must match supervisor.EXIT_RATE_LIMITED. 75 is EX_TEMPFAIL from
# sysexits.h -- "temporary failure, the user is invited to retry".
#
# Two kinds of 429 arrive here and they need very different waits:
#
#   75  Cloudflare's error 1015. An edge ban on the source IP that lifts
#       only once that address goes quiet, so the wait is long.
#   76  Discord's own "exceeding global rate limits". An application-layer
#       block, minutes rather than half an hour -- one observed on
#       2026-08-18 lasted about nine. Holding every bot down for thirty
#       minutes over it wastes most of the outage on something that had
#       already cleared.
#
# They are told apart by their body: Cloudflare serves an HTML block page,
# Discord answers with JSON.
EXIT_RATE_LIMITED = 75
EXIT_RATE_LIMITED_BRIEF = 76


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


def is_edge_ban(page: str) -> bool:
    """True if this 429 is Cloudflare turning us away, not Discord.

    Cloudflare answers with a full HTML error page; Discord's own rate
    limiter answers with JSON. The distinction decides how long every bot
    has to stay down, so it is made on the shape of the body rather than
    on the status code, which is 429 either way.
    """
    lowered = (page or "").lower()
    return "<html" in lowered or "cf-error" in lowered or "error 1015" in lowered


# A 429 that arrives after a successful login never reaches run(), so it
# never becomes an exit code and the supervisor never hears about it.
# These decide when enough of them have arrived to call it a sustained
# block rather than one unlucky request.
#
# Three inside two minutes: a member retrying a dead command produces
# roughly that (the 2026-08-19 log shows three in 86 seconds), while
# ordinary traffic on a healthy address produces none.
RUNTIME_THRESHOLD = 3
RUNTIME_WINDOW = 120.0


class RuntimeRateLimit:
    """Decides when post-login 429s mean the bot should stop knocking.

    supervisor.py's cooldowns rest on the conclusion that this block only
    lifts once the address goes quiet -- and that retrying through it
    keeps it alive. That reasoning does not stop applying just because the
    bot happened to be logged in when the block started, but until this
    existed there was no way to act on it: the bot stayed up and kept
    issuing requests for as long as members kept typing.

    Deliberately a plain counter with an injected clock rather than
    anything time-aware of its own, so the window can be tested without
    sleeping through it.
    """

    def __init__(
        self,
        threshold: int = RUNTIME_THRESHOLD,
        window: float = RUNTIME_WINDOW,
        clock=time.monotonic,
    ):
        self._threshold = threshold
        self._window = window
        self._clock = clock
        self._hits: list[float] = []

    def record(self) -> bool:
        """Note one post-login 429. True when the bot should go quiet.

        The window slides rather than resetting: a block does not restart
        its clock politely, so what matters is how many hits are inside
        the last `window` seconds, not when the first of them landed.
        """
        now = self._clock()
        self._hits = [hit for hit in self._hits if now - hit < self._window]
        self._hits.append(now)
        return len(self._hits) >= self._threshold


def is_command_rate_limited(error: BaseException) -> bool:
    """True if this failure is Discord rate limiting us.

    Accepts the wrapped form too: discord.py hands an error handler a
    CommandInvokeError carrying the real exception in .original.
    """
    return is_rate_limited(getattr(error, "original", error))


class QuietOnBlock:
    """Closes a bot once post-login 429s stop looking like bad luck.

    Closing rather than sleeping in place, because the supervisor already
    owns the "hold every child off this address" policy and only learns of
    a block through an exit code. Routing both kinds of block through the
    same door keeps one policy instead of two that can drift apart.

    One instance per bot; `exit_code` is what the process should exit with
    once run() returns.
    """

    def __init__(self, bot, tag: str, watch: "RuntimeRateLimit | None" = None):
        self._bot = bot
        self._tag = tag
        self._watch = watch if watch is not None else RuntimeRateLimit()
        self.going_quiet = False

    @property
    def exit_code(self) -> int:
        return EXIT_RATE_LIMITED_BRIEF if self.going_quiet else 0

    async def note(self, error: BaseException) -> None:
        """Record one command failure, closing the bot if it is time to stop.

        The going_quiet guard matters: discord.py's close() is not
        idempotent, and every command still queued behind the block would
        otherwise call it again against a loop already shutting down.
        """
        if self.going_quiet or not is_command_rate_limited(error):
            return
        if not self._watch.record():
            return
        self.going_quiet = True
        print(
            f"{self._tag} Discord is still rate limiting this instance after "
            "login. Closing so the supervisor can hold every bot off until "
            "the block on this address lifts.",
            file=sys.stderr,
            flush=True,
        )
        await self._bot.close()


def run(bot, token: str, **kwargs) -> None:
    """bot.run(token), converting an IP ban into EXIT_RATE_LIMITED."""
    try:
        bot.run(token, **kwargs)
    except discord.HTTPException as exc:
        if not is_rate_limited(exc):
            raise
        if not is_edge_ban(exc.text):
            # Discord's own limiter. Short-lived, and its JSON body is one
            # readable sentence, so it is worth printing verbatim.
            print(
                f"Discord applied a global rate limit to this instance "
                f"(HTTP 429): {exc.text.strip()} Exiting so the supervisor "
                "can hold every bot off briefly.",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(EXIT_RATE_LIMITED_BRIEF)

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
