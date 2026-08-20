"""Run the timer bot and the attendance bot as independent processes.

The timer (bot.py) is in production and must not be put at risk by the
attendance feature. Keeping them in separate OS processes means an import
error, an unhandled exception, a blocked event loop, or an out-of-memory
kill in one cannot stop the other.

Two free Render services would have isolated them further, but Render's
750 free instance hours are shared across a workspace: two services
running 24/7 exhaust them mid-month, and Render then suspends *every*
free service -- including the timer. One service, two processes, stays
inside the budget.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Exit codes meaning "I stopped on purpose, leave me alone".
# 78 is EX_CONFIG from sysexits.h -- the attendance bot uses it when its
# credentials are missing, so deploying before they exist is harmless.
#
# This is the DEFAULT policy, used by ChildSpec.no_restart_codes unless a
# spec overrides it. It must NOT be applied to the timer: bot.py exits 0
# whenever bot.run() returns normally (not just when deliberately stopped),
# so treating 0 as "leave it stopped" would mean an ordinary timer exit is
# never relaunched. The timer's ChildSpec below overrides this with an
# empty set -- it always restarts, no matter the exit code.
EXIT_NOT_CONFIGURED = 78

# 75 is EX_TEMPFAIL: the bot could not reach Discord because Discord's
# edge is rate-limiting this instance's IP (Cloudflare error 1015). It is
# a restartable code -- the ban lifts on its own -- but not until the IP
# goes quiet, which is what RATE_LIMIT_COOLDOWN below is for. It must
# therefore never appear in any no_restart_codes set.
EXIT_RATE_LIMITED = 75

# 76 is the same situation with a much shorter fuse: Discord's own
# "exceeding global rate limits" rather than a Cloudflare edge ban. It is
# an application-layer block measured in minutes -- one observed on
# 2026-08-18 lasted about nine -- so holding every bot for the full
# RATE_LIMIT_COOLDOWN spends most of the outage waiting on something that
# has already cleared. Restartable, like 75.
EXIT_RATE_LIMITED_BRIEF = 76

NO_RESTART_CODES = frozenset({0, EXIT_NOT_CONFIGURED})

POLL_INTERVAL = 2.0

# Restart backoff: a permanently broken child would otherwise restart
# roughly every POLL_INTERVAL-ish seconds forever -- tens of thousands of
# interpreter starts a day on a 0.1 CPU instance, competing with the
# timer for the same shared resources. Each consecutive crash doubles the
# delay up to RESTART_DELAY_CAP; a child that stays up for
# RESTART_RESET_AFTER seconds is considered healthy again and its delay
# resets to the base.
RESTART_DELAY_CAP = 300.0  # 5 minutes
RESTART_RESET_AFTER = 60.0

# How long every child stays down after any one of them reports
# EXIT_RATE_LIMITED. Cloudflare's 1015 ban is keyed on the source IP and
# lifts once that IP stops knocking, so the wait has to be service-wide:
# the three bots share one egress IP, and a sibling logging in every few
# seconds refreshes a ban that none of them can then get past. 30 minutes
# is chosen to comfortably outlast a 1015; the cost of overshooting is a
# late restart, the cost of undershooting is a loop that never ends.
RATE_LIMIT_COOLDOWN = 1800.0  # 30 minutes

# For EXIT_RATE_LIMITED_BRIEF. Started at five minutes, on the reasoning
# that Discord's global limit clears far faster than a Cloudflare ban and
# a short wait would cost less. The block on 2026-08-18 outlasted that
# assumption badly -- over ninety minutes, with a retry every five --
# which raises the possibility that the retries were themselves keeping
# it alive, exactly as the 1015 earlier that day only lifted once the
# address went quiet.
#
# So it now matches RATE_LIMIT_COOLDOWN: whichever kind of 429 arrives,
# the address goes properly silent before knocking again. The two remain
# separate constants, and the two exit codes still log which block was in
# force, so this can be lowered again if a shorter wait proves safe.
BRIEF_RATE_LIMIT_COOLDOWN = 1800.0  # 30 minutes


DEFAULT_KEEPALIVE_PORT = 8080

# Where the running log goes, and how big it may get.
#
# Render shows stdout in a dashboard and systemd hands it to journald,
# but Windows Task Scheduler simply discards it -- so on the PC this now
# runs on, every line the three bots print would vanish. There is no
# console to read and nobody who could read one. A file is the only
# record its owner can be asked to copy back.
#
# Two files of LOG_MAX_BYTES, so the whole thing is bounded on a machine
# nobody maintains: months of gateway chatter must not fill a disk.
DEFAULT_LOG_FILE = Path(__file__).with_name("logs") / "supervisor.log"
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB, plus one rotated copy


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: list[str]
    no_restart_codes: frozenset[int] = NO_RESTART_CODES


def start_keepalive(supervisor, port: int | None = None):
    """Bind $PORT and answer pings, for as long as the supervisor lives.

    Render only keeps a free web service awake while something answers
    HTTP on $PORT. This listener used to live in bot.py's setup_hook,
    which runs only after a successful Discord login -- so the night
    Discord became unreachable, no bot ever logged in, nothing bound the
    port, Render found a dead service and spun it down. The bots could
    not come back even once Discord recovered, because the instance was
    asleep and could not be woken.

    It belongs here because the supervisor is the one process that stays
    up precisely when the children cannot: a crash-looping bot no longer
    takes the whole service down with it.

    Returns the server, or None if the port could not be bound. A failed
    bind is deliberately not fatal -- the bots matter more than the ping,
    and refusing to start them over a busy port would trade a service
    that sleeps for one that never runs at all.
    """
    if port is None:
        port = int(os.getenv("PORT", str(DEFAULT_KEEPALIVE_PORT)))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            running = supervisor.running_names()
            body = (
                "fb-timer supervisor alive\n"
                f"running: {', '.join(running) if running else 'none'}\n"
            ).encode()
            # 200 even with no child running: this answers "is the service
            # up", and the supervisor IS up and restarting them. A 503 here
            # would let Render sleep the instance during the exact outage
            # this endpoint exists to survive.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            # An uptime pinger hits this every few minutes; the default
            # handler would write a line per request to stderr and bury
            # the supervisor's own output.
            pass

    # Only Render sets $PORT, and only Render's health check reaches this
    # from off-box. Everywhere else -- a VPS, a guildmate's PC -- binding
    # every interface buys nothing and costs a Windows Defender firewall
    # prompt on first run: an administrator dialog, during an unattended
    # install, in front of someone who cannot tell it from a virus
    # warning. Localhost keeps the endpoint for debugging without it.
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"[supervisor] keep-alive not started (port {port}): {exc}", flush=True)
        return None

    thread = threading.Thread(
        target=server.serve_forever, name="keepalive", daemon=True
    )
    thread.start()
    print(f"[supervisor] keep-alive listening on {host}:{port}", flush=True)
    return server


def stop_keepalive(server) -> None:
    """Shut the keep-alive server down. Safe to call with None."""
    if server is None:
        return
    server.shutdown()
    server.server_close()


def should_restart(exit_code: int, no_restart_codes: frozenset[int] = NO_RESTART_CODES) -> bool:
    """True if a child that exited with this code should be relaunched."""
    return exit_code not in no_restart_codes


class Supervisor:
    """Starts child processes and relaunches the ones that crash."""

    def __init__(
        self,
        specs: list[ChildSpec],
        *,
        restart_delay: float = 5.0,
        max_restart_delay: float = RESTART_DELAY_CAP,
        restart_reset_after: float = RESTART_RESET_AFTER,
        rate_limit_cooldown: float = RATE_LIMIT_COOLDOWN,
        brief_rate_limit_cooldown: float = BRIEF_RATE_LIMIT_COOLDOWN,
        log_file: "Path | None" = None,
        max_log_bytes: int = LOG_MAX_BYTES,
    ):
        self._specs = {spec.name: spec for spec in specs}
        self._procs: dict[str, subprocess.Popen] = {}
        self._restart_delay = restart_delay
        self._max_restart_delay = max_restart_delay
        self._restart_reset_after = restart_reset_after
        self._rate_limit_cooldown = rate_limit_cooldown
        self._brief_rate_limit_cooldown = brief_rate_limit_cooldown
        self._stopping = False
        # Monotonic time before which NO child may be launched, set when
        # any one of them exits with EXIT_RATE_LIMITED. Service-wide on
        # purpose: the ban is on the shared egress IP, not on the bot
        # that happened to notice it.
        self._cooldown_until = 0.0
        # name -> monotonic time it becomes eligible for relaunch. A dead
        # child lives here (not in self._procs) while its restart delay
        # elapses, so waiting it out never blocks tick() from noticing a
        # sibling die in the meantime.
        self._due_at: dict[str, float] = {}
        # name -> the delay to use for that child's NEXT restart, doubling
        # on every consecutive crash up to _max_restart_delay. Absent
        # means "use the base restart_delay".
        self._current_delay: dict[str, float] = {}
        # name -> monotonic time it was last launched, used to tell a
        # child that just crashed apart from one that ran healthily for a
        # while before crashing (which resets its backoff).
        self._launched_at: dict[str, float] = {}
        self._log_file = Path(log_file) if log_file is not None else None
        self._max_log_bytes = max_log_bytes
        # One writer at a time: a thread per child pumps its output here,
        # and rotation renames the file out from under all of them.
        self._log_lock = threading.Lock()
        if self._log_file is not None:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)

    # -- logging ----------------------------------------------------------

    def log(self, line: str) -> None:
        """Report one line to stdout and, if configured, to the log file.

        Always stdout as well, never instead: Render reads stdout and
        journald reads stdout, and neither will ever look at this file.
        """
        print(line, flush=True)
        if self._log_file is None:
            return
        with self._log_lock:
            try:
                self._rotate_if_needed()
                with self._log_file.open("a", encoding="utf-8", errors="replace") as fh:
                    fh.write(line.rstrip("\n") + "\n")
            except OSError:
                # A full or unwritable disk must not take the bots down;
                # stdout above already carried the line.
                pass

    def _rotate_if_needed(self) -> None:
        """Keep at most two files. Caller holds _log_lock."""
        try:
            if self._log_file.stat().st_size < self._max_log_bytes:
                return
        except FileNotFoundError:
            return
        previous = self._log_file.with_suffix(self._log_file.suffix + ".1")
        try:
            previous.unlink()
        except FileNotFoundError:
            pass
        self._log_file.rename(previous)

    def _pump(self, name: str, stream) -> None:
        """Forward one child's output, line by line, until it closes.

        The children are separate processes, so their output never passes
        through this one's sys.stdout -- capturing it means reading their
        pipe. That is the whole point: the lines worth having ("[items]
        restored state", a traceback, discord.py's rate-limit warnings)
        are theirs, not the supervisor's.
        """
        try:
            for line in stream:
                self.log(line.rstrip())
        except (ValueError, OSError):
            # The pipe closed under us while the child was being killed.
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    # -- inspection -------------------------------------------------------

    def running_names(self) -> list[str]:
        return [name for name, proc in self._procs.items() if proc.poll() is None]

    def pid_of(self, name: str) -> int | None:
        proc = self._procs.get(name)
        return proc.pid if proc is not None else None

    # -- lifecycle --------------------------------------------------------

    def _launch(self, name: str) -> None:
        spec = self._specs[name]
        self.log(f"[supervisor] starting {name}: {' '.join(spec.argv)}")
        proc = subprocess.Popen(
            spec.argv,
            env=os.environ.copy(),
            # Piped only so the output can be captured on the way past.
            # stderr folded into stdout keeps interleaving intact, which
            # matters when a traceback follows the line that caused it.
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
        self._procs[name] = proc
        threading.Thread(
            target=self._pump, args=(name, proc.stdout), daemon=True
        ).start()
        self._launched_at[name] = time.monotonic()

    def start_all(self) -> None:
        for name in self._specs:
            self._launch(name)

    def tick(self) -> list[str]:
        """Check every child once; relaunch the ones that are due.

        Never blocks: a crashed child's restart delay only postpones that
        child's own relaunch by recording when it becomes due, instead of
        sleeping inline. Sleeping here would stall detection of everyone
        else for up to restart_delay seconds -- including a still-healthy
        sibling like the timer -- which defeats the point of running them
        as independent processes.

        Returns the names restarted, so callers and tests can see what
        happened without parsing logs.
        """
        now = time.monotonic()

        for name, proc in list(self._procs.items()):
            code = proc.poll()
            if code is None:
                continue

            del self._procs[name]
            spec = self._specs[name]
            if not should_restart(code, spec.no_restart_codes):
                self.log(
                    f"[supervisor] {name} exited with {code}; leaving it stopped"
                )
                self._due_at.pop(name, None)
                self._current_delay.pop(name, None)
                continue

            if code in (EXIT_RATE_LIMITED, EXIT_RATE_LIMITED_BRIEF):
                cooldown = (
                    self._rate_limit_cooldown
                    if code == EXIT_RATE_LIMITED
                    else self._brief_rate_limit_cooldown
                )
                self._cooldown_until = max(self._cooldown_until, now + cooldown)
                self.log(
                    f"[supervisor] {name} was rate-limited by Discord; "
                    f"holding every bot for {cooldown}s so "
                    "the block on this IP can lift"
                )

            uptime = now - self._launched_at.get(name, now)
            if uptime >= self._restart_reset_after:
                # It stayed up long enough to count as healthy; forget any
                # backoff accumulated from an earlier crash-loop.
                self._current_delay.pop(name, None)

            delay = self._current_delay.get(name, self._restart_delay)
            if delay >= self._max_restart_delay:
                self.log(
                    f"[supervisor] {name} is crash-looping; capping restart "
                    f"delay at {self._max_restart_delay}s"
                )
            else:
                self.log(
                    f"[supervisor] {name} exited with {code}; restarting in "
                    f"{delay}s"
                )
            self._due_at[name] = now + delay
            self._current_delay[name] = min(delay * 2, self._max_restart_delay)

        restarted = []
        if now < self._cooldown_until:
            # Every child stays queued in self._due_at. Children already
            # running are deliberately left alone: the ban bites on login,
            # so a bot that is connected is still doing its job.
            return restarted
        for name, due in list(self._due_at.items()):
            if now >= due:
                del self._due_at[name]
                self._launch(name)
                restarted.append(name)
        return restarted

    def stop_all(self, timeout: float = 10.0) -> None:
        self._stopping = True
        for name, proc in self._procs.items():
            if proc.poll() is None:
                self.log(f"[supervisor] stopping {name}")
                proc.terminate()

        deadline = time.monotonic() + timeout
        for proc in self._procs.values():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._procs.clear()
        self._due_at.clear()
        self._current_delay.clear()
        self._cooldown_until = 0.0

    def run(self) -> None:
        """Start everything and supervise until told to stop."""

        def handle_signal(signum, _frame):
            if self._stopping:
                # A second SIGTERM/SIGINT while shutdown is already under
                # way -- orchestrators (Render included) escalate like
                # this. stop_all() may be mid-flight, blocked waiting on a
                # child; re-entering it here would mutate self._procs out
                # from under its own iteration. Ignore and let the first
                # call finish.
                self.log(
                    f"[supervisor] received signal {signum} while already "
                    "stopping; ignoring"
                )
                return
            self.log(f"[supervisor] received signal {signum}")
            self.stop_all()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        # Before the children, not after: the port must be answering even
        # if every bot crashes on its first attempt, which is exactly the
        # case that used to let Render sleep the whole service.
        keepalive = start_keepalive(self)
        try:
            self._supervise()
        finally:
            stop_keepalive(keepalive)

    def _supervise(self) -> None:
        self.start_all()
        while not self._stopping:
            time.sleep(POLL_INTERVAL)
            self.tick()
            # A child scheduled to restart lives in self._due_at, not
            # self._procs, for the length of its restart delay -- it must
            # count as "still here" or a crash right before a long delay
            # would empty self._procs and exit the supervisor mid-wait,
            # abandoning the very restart it just scheduled.
            if not self._procs and not self._due_at:
                self.log("[supervisor] no children left; exiting")
                return


CHILDREN = [
    # bot.py exits 0 whenever bot.run() returns normally -- not only when
    # deliberately stopped -- so the timer must always be relaunched,
    # regardless of exit code.
    ChildSpec(
        "timer", [sys.executable, "-u", "bot.py"], no_restart_codes=frozenset()
    ),
    ChildSpec("attendance", [sys.executable, "-u", "attendance_bot.py"]),
    ChildSpec("items", [sys.executable, "-u", "items_bot.py"]),
]


if __name__ == "__main__":
    # A log file in production, not only in tests: this is the only
    # record that exists on a Windows host, where Task Scheduler throws
    # stdout away and there is no console to read.
    Supervisor(CHILDREN, log_file=DEFAULT_LOG_FILE).run()
