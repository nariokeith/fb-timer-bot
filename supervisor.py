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

import http.client
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

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


DEFAULT_KEEPALIVE_PORT = 8080

# Render sleeps a free instance after roughly 15 minutes without an
# inbound request, and a sleeping instance sends no spawn notifications.
# 10 minutes leaves room for one ping to fail outright and the next to
# still land inside the window.
# Five minutes, not ten: a ping can fail outright -- the dead edge
# address guarantees some will -- and two consecutive misses still leave
# the next attempt inside Render's ~15-minute window.
SELF_PING_INTERVAL = 300.0
SELF_PING_TIMEOUT = 10.0


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

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        print(f"[supervisor] keep-alive not started (port {port}): {exc}", flush=True)
        return None

    thread = threading.Thread(
        target=server.serve_forever, name="keepalive", daemon=True
    )
    thread.start()
    print(f"[supervisor] keep-alive listening on port {port}", flush=True)
    return server


def stop_keepalive(server) -> None:
    """Shut the keep-alive server down. Safe to call with None."""
    if server is None:
        return
    server.shutdown()
    server.server_close()


def _fetch_status(address: str, *, host: str, timeout: float) -> int:
    """GET https://<address>/ while presenting `host` for SNI and routing.

    Connecting by address rather than by name is the entire point: the
    hostname resolves to several edge addresses and this walks them, so
    one dead listener cannot decide the outcome. The name still has to
    travel in SNI and in the Host header or the edge cannot route it and
    the certificate cannot verify.
    """
    context = ssl.create_default_context()
    raw = socket.create_connection((address, 443), timeout=timeout)
    try:
        sock = context.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise

    connection = http.client.HTTPSConnection(host, timeout=timeout)
    connection.sock = sock
    try:
        connection.request("GET", "/", headers={"Host": host})
        return connection.getresponse().status
    finally:
        connection.close()


def _resolve(host: str, port: int) -> list[str]:
    seen = []
    for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def ping_once(
    url: str,
    *,
    timeout: float = SELF_PING_TIMEOUT,
    addresses: list[str] | None = None,
    resolve=_resolve,
    fetch=_fetch_status,
) -> bool:
    """Fetch `url`, trying every address it resolves to. True if one answered.

    Never raises: this runs on a background thread whose death would be
    silent, and a failed ping is not worth taking anything else down for.
    """
    host = urlsplit(url).hostname
    if not host:
        return False

    if addresses is None:
        try:
            addresses = resolve(host, 443)
        except Exception as exc:
            print(f"[supervisor] self-ping could not resolve {host}: {exc}", flush=True)
            return False

    for address in addresses:
        try:
            status = fetch(address, host=host, timeout=timeout)
        except Exception:
            # Expected for a dead listener; the next address is the answer.
            continue
        if 200 <= status < 400:
            return True
    return False


def report_ping(ok: bool, url: str, elapsed: float) -> None:
    """Print the outcome of one self-ping.

    Successes are printed too, not just failures. A ping that only
    speaks up when it breaks leaves "the instance never slept" as an
    unfalsifiable claim -- there is no way to tell a working loop from a
    thread that died quietly. One line every SELF_PING_INTERVAL is a
    price worth paying for that.
    """
    if ok:
        print(f"[supervisor] self-ping ok ({elapsed:.1f}s) {url}", flush=True)
    else:
        print(f"[supervisor] self-ping failed ({elapsed:.1f}s) {url}", flush=True)


def start_self_ping(url: str | None = None, *, ping=ping_once):
    """Keep the instance awake by requesting its own public URL.

    Returns the thread, or None when there is no public URL -- off the
    platform there is nothing to keep awake and nothing to request.

    The traffic leaves the instance and comes back through Render's edge,
    so it counts as the inbound activity the spin-down timer watches. It
    exists alongside an external monitor rather than instead of one: this
    can only PREVENT a sleep, never end one, so a monitor is still what
    wakes the service if it ever does go down.
    """
    if url is None:
        url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return None

    stop = threading.Event()

    def loop():
        # Ping immediately, then on the interval. Waiting the interval out
        # first left the instance unguarded for that long after every
        # deploy -- the very moment it is most likely to be idle, because
        # the container is new and no visitor has arrived yet.
        while True:
            started = time.monotonic()
            ok = ping(url)
            report_ping(ok, url, time.monotonic() - started)
            if stop.wait(SELF_PING_INTERVAL):
                return

    thread = threading.Thread(target=loop, name="self-ping", daemon=True)
    thread._stop_event = stop
    thread.start()
    print(f"[supervisor] self-ping every {SELF_PING_INTERVAL}s to {url}", flush=True)
    return thread


def stop_self_ping(thread) -> None:
    """Stop the self-ping thread. Safe to call with None."""
    if thread is None:
        return
    thread._stop_event.set()
    thread.join(timeout=5.0)


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
    ):
        self._specs = {spec.name: spec for spec in specs}
        self._procs: dict[str, subprocess.Popen] = {}
        self._restart_delay = restart_delay
        self._max_restart_delay = max_restart_delay
        self._restart_reset_after = restart_reset_after
        self._rate_limit_cooldown = rate_limit_cooldown
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

    # -- inspection -------------------------------------------------------

    def running_names(self) -> list[str]:
        return [name for name, proc in self._procs.items() if proc.poll() is None]

    def pid_of(self, name: str) -> int | None:
        proc = self._procs.get(name)
        return proc.pid if proc is not None else None

    # -- lifecycle --------------------------------------------------------

    def _launch(self, name: str) -> None:
        spec = self._specs[name]
        print(f"[supervisor] starting {name}: {' '.join(spec.argv)}", flush=True)
        self._procs[name] = subprocess.Popen(spec.argv, env=os.environ.copy())
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
                print(
                    f"[supervisor] {name} exited with {code}; leaving it stopped",
                    flush=True,
                )
                self._due_at.pop(name, None)
                self._current_delay.pop(name, None)
                continue

            if code == EXIT_RATE_LIMITED:
                self._cooldown_until = max(
                    self._cooldown_until, now + self._rate_limit_cooldown
                )
                print(
                    f"[supervisor] {name} was rate-limited by Discord; "
                    f"holding every bot for {self._rate_limit_cooldown}s so "
                    "the ban on this IP can lift",
                    flush=True,
                )

            uptime = now - self._launched_at.get(name, now)
            if uptime >= self._restart_reset_after:
                # It stayed up long enough to count as healthy; forget any
                # backoff accumulated from an earlier crash-loop.
                self._current_delay.pop(name, None)

            delay = self._current_delay.get(name, self._restart_delay)
            if delay >= self._max_restart_delay:
                print(
                    f"[supervisor] {name} is crash-looping; capping restart "
                    f"delay at {self._max_restart_delay}s",
                    flush=True,
                )
            else:
                print(
                    f"[supervisor] {name} exited with {code}; restarting in "
                    f"{delay}s",
                    flush=True,
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
                print(f"[supervisor] stopping {name}", flush=True)
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
                print(
                    f"[supervisor] received signal {signum} while already "
                    "stopping; ignoring",
                    flush=True,
                )
                return
            print(f"[supervisor] received signal {signum}", flush=True)
            self.stop_all()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        # Before the children, not after: the port must be answering even
        # if every bot crashes on its first attempt, which is exactly the
        # case that used to let Render sleep the whole service.
        keepalive = start_keepalive(self)
        self_ping = start_self_ping()
        try:
            self._supervise()
        finally:
            stop_self_ping(self_ping)
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
                print("[supervisor] no children left; exiting", flush=True)
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
    Supervisor(CHILDREN).run()
