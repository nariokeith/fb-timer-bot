import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import supervisor as supervisor_mod
from supervisor import (
    CHILDREN,
    EXIT_NOT_CONFIGURED,
    EXIT_RATE_LIMITED,
    NO_RESTART_CODES,
    RATE_LIMIT_COOLDOWN,
    ChildSpec,
    Supervisor,
    should_restart,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


SLEEP_FOREVER = _python("import time; time.sleep(300)")
EXIT_CRASH = _python("import sys; sys.exit(1)")
EXIT_CLEAN = _python("import sys; sys.exit(0)")
EXIT_UNCONFIGURED = _python(f"import sys; sys.exit({EXIT_NOT_CONFIGURED})")

# Stays up briefly (long enough to clear a short restart_reset_after in a
# test) before crashing, so backoff-reset can be exercised without waiting
# out the production-sized RESTART_RESET_AFTER.
CRASH_AFTER_BRIEF_UPTIME = _python("import sys, time; time.sleep(0.5); sys.exit(1)")

# A child that survives SIGTERM for a little while instead of dying
# instantly -- widens the window in which a *second* signal to the
# supervisor can land while stop_all() is still mid-shutdown for it.
SLOW_TO_DIE_ON_TERM = _python(
    "import signal, sys, time\n"
    "def _h(*_a):\n"
    "    time.sleep(1.5)\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, _h)\n"
    "time.sleep(300)\n"
)


def _settle():
    """Give a short-lived child time to exit before polling."""
    time.sleep(0.4)


def _wait_until(predicate, timeout=5.0, interval=0.05):
    """Poll `predicate` until it's true or `timeout` elapses.

    Preferred over a bare sleep for anything timing-dependent: returns as
    soon as the condition holds instead of always waiting the worst case.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _child_pids(pid):
    """Live child PIDs of `pid`, via `pgrep -P` (POSIX; works on macOS and Linux)."""
    result = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True
    )
    return [int(p) for p in result.stdout.split()]


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_the_item_bot_is_supervised():
    names = [spec.name for spec in CHILDREN]
    assert "items" in names


def test_the_item_bot_stays_stopped_when_not_configured():
    """Exit 78 must not crash-loop.

    A crash-looping child on a 0.1 CPU instance competes with the timer
    for the same shared resources, which is exactly what the default
    NO_RESTART_CODES policy exists to prevent.
    """
    spec = next(s for s in CHILDREN if s.name == "items")
    assert not should_restart(EXIT_NOT_CONFIGURED, spec.no_restart_codes)


def _run_harness(child_name, child_argv, restart_delay=0):
    """Spawn `supervisor.py`'s real Supervisor.run() as its own OS process.

    Used to test behaviour of run() for real -- signal handling, the
    poll/relaunch loop -- against a genuine subprocess rather than calling
    private pieces (like the signal handler) directly in-process, which
    would miss bugs that only show up in run()'s own control flow (an
    orphaned child, or the supervisor exiting early).
    """
    code = (
        "from supervisor import ChildSpec, Supervisor\n"
        f"Supervisor([ChildSpec({child_name!r}, {child_argv!r})], "
        f"restart_delay={restart_delay!r}).run()\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.fixture
def stopper():
    created = []
    yield created
    for sup in created:
        sup.stop_all(timeout=2.0)


def test_a_crashed_child_is_restarted():
    assert should_restart(1) is True
    assert should_restart(137) is True


def test_a_deliberate_exit_is_not_restarted():
    assert should_restart(0) is False
    assert should_restart(EXIT_NOT_CONFIGURED) is False


def test_start_all_launches_every_child(stopper):
    sup = Supervisor(
        [ChildSpec("a", SLEEP_FOREVER), ChildSpec("b", SLEEP_FOREVER)],
        restart_delay=0,
    )
    stopper.append(sup)
    sup.start_all()
    assert sorted(sup.running_names()) == ["a", "b"]


def test_a_crashing_child_is_restarted_on_tick(stopper):
    sup = Supervisor([ChildSpec("flaky", EXIT_CRASH)], restart_delay=0)
    stopper.append(sup)
    sup.start_all()
    _settle()
    assert sup.tick() == ["flaky"]


def test_a_child_that_exits_cleanly_is_left_alone(stopper):
    sup = Supervisor([ChildSpec("done", EXIT_CLEAN)], restart_delay=0)
    stopper.append(sup)
    sup.start_all()
    _settle()
    assert sup.tick() == []
    assert sup.running_names() == []


def test_an_unconfigured_child_is_left_stopped(stopper):
    sup = Supervisor([ChildSpec("attendance", EXIT_UNCONFIGURED)], restart_delay=0)
    stopper.append(sup)
    sup.start_all()
    _settle()
    assert sup.tick() == []
    assert sup.running_names() == []


def test_one_child_dying_does_not_disturb_the_other(stopper):
    """The property this whole design exists for."""
    sup = Supervisor(
        [ChildSpec("timer", SLEEP_FOREVER), ChildSpec("attendance", EXIT_CRASH)],
        restart_delay=0,
    )
    stopper.append(sup)
    sup.start_all()
    timer_pid = sup.pid_of("timer")

    _settle()
    assert sup.tick() == ["attendance"]

    assert "timer" in sup.running_names()
    assert sup.pid_of("timer") == timer_pid  # never restarted


def test_an_unconfigured_child_does_not_disturb_the_other(stopper):
    """Deploying before the attendance secrets exist must be safe."""
    sup = Supervisor(
        [
            ChildSpec("timer", SLEEP_FOREVER),
            ChildSpec("attendance", EXIT_UNCONFIGURED),
        ],
        restart_delay=0,
    )
    stopper.append(sup)
    sup.start_all()
    timer_pid = sup.pid_of("timer")

    _settle()
    assert sup.tick() == []
    assert sup.running_names() == ["timer"]
    assert sup.pid_of("timer") == timer_pid


def test_stop_all_terminates_everything():
    sup = Supervisor([ChildSpec("a", SLEEP_FOREVER)], restart_delay=0)
    sup.start_all()
    sup.stop_all(timeout=2.0)
    assert sup.running_names() == []


def test_tick_never_blocks_on_a_siblings_restart_delay(stopper):
    """The property this whole design exists for, under the real default.

    Every other test uses restart_delay=0, which never exercises the
    blocking behaviour of the production default (5s). Here attendance
    crashes with a real, non-zero restart_delay: tick() must return
    immediately -- without waiting out attendance's delay -- so a crash of
    the timer itself would still be caught on this same call.
    """
    sup = Supervisor(
        [ChildSpec("timer", SLEEP_FOREVER), ChildSpec("attendance", EXIT_CRASH)],
        restart_delay=1.0,
    )
    stopper.append(sup)
    sup.start_all()
    timer_pid = sup.pid_of("timer")
    _settle()

    started = time.monotonic()
    restarted = sup.tick()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, "tick() blocked instead of deferring the restart"
    assert restarted == []  # attendance isn't due for relaunch yet
    assert sup.running_names() == ["timer"]
    assert sup.pid_of("timer") == timer_pid  # sibling never touched

    # ...and it does eventually come back once its delay elapses, on a
    # later, still-non-blocking tick() -- restart deferred, not dropped.
    assert _wait_until(lambda: sup.tick() == ["attendance"], timeout=3.0)
    assert "attendance" in sup.running_names()


def test_run_handles_sigterm_and_leaves_no_orphans():
    """The literal mechanism Render uses to stop this process.

    No test drove Supervisor.run()'s signal wiring before -- every other
    test calls stop_all() directly. This spawns supervisor.py's real
    Supervisor.run() as its own OS process, sends it a genuine SIGTERM (not
    a direct call to the handler), and checks both that the harness exits
    cleanly and that its child is actually gone -- no orphan left behind.
    """
    harness = _run_harness("child", SLEEP_FOREVER)
    try:
        assert _wait_until(lambda: len(_child_pids(harness.pid)) == 1, timeout=5.0)
        child_pid = _child_pids(harness.pid)[0]

        harness.send_signal(signal.SIGTERM)

        assert _wait_until(lambda: harness.poll() is not None, timeout=8.0)
        assert harness.returncode == 0

        assert _wait_until(lambda: not _pid_alive(child_pid), timeout=3.0)
    finally:
        if harness.poll() is None:
            harness.kill()
            harness.wait(timeout=2)


def test_a_second_signal_during_shutdown_does_not_crash():
    """Orchestrators commonly escalate with repeated signals.

    The child here absorbs SIGTERM but takes 1.5s to actually exit, which
    holds the harness inside stop_all()'s wait loop long enough for a
    second, real SIGTERM to land on the harness process itself while
    shutdown is still in progress -- exactly the reentrant case the guard
    in handle_signal exists for. It must not crash and must not do
    another full teardown pass.

    (Empirically, CPython's PEP 475 signal/retry semantics make the
    reviewer-described `RuntimeError: dictionary changed size during
    iteration` hard to force deterministically over real OS signals --
    verified by hand against the pre-fix code with 1- and 2-child
    harnesses at several timings: every run unwound cleanly via the
    reentrant call's own SystemExit(0) instead. The `no RuntimeError`
    assertion below is kept as the belt to the guard's suspenders; the
    `"ignoring"` assertion is what actually pins the fix -- it can only
    appear once handle_signal checks self._stopping, so it is false on
    the pre-fix code and true after.)
    """
    harness = _run_harness("slow", SLOW_TO_DIE_ON_TERM)
    try:
        assert _wait_until(lambda: len(_child_pids(harness.pid)) == 1, timeout=5.0)
        child_pid = _child_pids(harness.pid)[0]

        harness.send_signal(signal.SIGTERM)
        time.sleep(0.3)  # land squarely inside the first shutdown's wait
        harness.send_signal(signal.SIGTERM)  # escalation

        assert _wait_until(lambda: harness.poll() is not None, timeout=8.0)
        stdout = harness.stdout.read()
        stderr = harness.stderr.read()
        assert "RuntimeError" not in stderr, stderr
        assert harness.returncode == 0, stderr

        assert "while already stopping; ignoring" in stdout, stdout
        # The guard means the second signal short-circuits instead of
        # tearing "slow" down a second time.
        assert stdout.count("[supervisor] stopping slow") == 1, stdout

        assert _wait_until(lambda: not _pid_alive(child_pid), timeout=3.0)
    finally:
        if harness.poll() is None:
            harness.kill()
            harness.wait(timeout=2)


def test_run_does_not_exit_while_a_restart_is_pending():
    """A crash isn't "no children left" -- it's "one child due back soon".

    run()'s loop-exit check used to be `if not self._procs`, written when
    a restart-pending child stayed in self._procs for the whole blocking
    sleep. Once tick() stopped blocking (moving a pending child out of
    self._procs and into self._due_at instead, see the earlier
    restart-delay fix), a lone child crashing with a non-zero
    restart_delay emptied self._procs for the whole delay window, and the
    old check would exit the supervisor mid-wait -- abandoning the very
    restart it had just scheduled. This is exactly what would happen in
    production the moment attendance_bot.py exits 78 (removed
    permanently, per design) and the timer then crashes once: a single
    timer crash would have silently taken the whole supervisor down.

    Must drive the real run() loop (not just tick()) -- the bug lives in
    run()'s exit condition, not in tick() itself.
    """
    restart_delay = 3.0
    harness = _run_harness("flaky", EXIT_CRASH, restart_delay=restart_delay)
    try:
        # The lone child crashes almost immediately and is due back in
        # `restart_delay` seconds. For that whole window self._procs is
        # empty but self._due_at is not -- exactly the gap the old exit
        # check missed. The supervisor must still be alive throughout it.
        gave_up_early = _wait_until(
            lambda: harness.poll() is not None, timeout=restart_delay + 1.0
        )
        assert not gave_up_early, "supervisor exited during the restart delay"

        # ...and the restart isn't just deferred, it actually happens. Not
        # checked by polling for the relaunched pid here: EXIT_CRASH exits
        # within tens of milliseconds of relaunch, so catching it alive
        # via pgrep is a coin flip. Instead just give the next tick --
        # which should trigger the relaunch -- time to actually fire; the
        # stdout-count assertion after the harness is torn down is what
        # deterministically proves the relaunch happened, by reading the
        # supervisor's own log rather than racing a live process.
        time.sleep(restart_delay + 1.0)
    finally:
        if harness.poll() is None:
            harness.terminate()
            try:
                harness.wait(timeout=3)
            except subprocess.TimeoutExpired:
                harness.kill()
                harness.wait(timeout=2)

    stdout = harness.stdout.read()
    assert "no children left; exiting" not in stdout, stdout
    assert stdout.count("[supervisor] starting flaky") >= 2, stdout


# -- per-ChildSpec restart policy (bot.py exits 0 on a normal return, so
#    the timer must restart on exit 0 even though attendance must not) ---


def test_children_have_the_correct_restart_policy():
    """CHILDREN is what actually ships; pin its policy directly."""
    specs = {c.name: c for c in CHILDREN}
    assert specs["timer"].no_restart_codes == frozenset()
    assert specs["attendance"].no_restart_codes == NO_RESTART_CODES


def test_a_child_with_the_timers_policy_is_restarted_after_exit_0(stopper):
    """bot.py exits 0 whenever bot.run() returns normally, not only when
    deliberately stopped -- so a timer-shaped ChildSpec (no_restart_codes
    empty) must relaunch on exit 0, unlike the default policy."""
    sup = Supervisor(
        [ChildSpec("timer", EXIT_CLEAN, no_restart_codes=frozenset())],
        restart_delay=0,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()
    assert sup.tick() == ["timer"]


def test_a_child_with_the_default_policy_is_not_restarted_after_exit_0(stopper):
    sup = Supervisor([ChildSpec("attendance", EXIT_CLEAN)], restart_delay=0)
    stopper.append(sup)
    sup.start_all()
    _settle()
    assert sup.tick() == []
    assert sup.running_names() == []


def test_78_still_stops_a_child_under_the_default_policy(stopper):
    sup = Supervisor([ChildSpec("attendance", EXIT_UNCONFIGURED)], restart_delay=0)
    stopper.append(sup)
    sup.start_all()
    _settle()
    assert sup.tick() == []
    assert sup.running_names() == []


# -- restart backoff -------------------------------------------------------


def test_restart_delay_doubles_on_consecutive_crashes(stopper):
    sup = Supervisor(
        [ChildSpec("flaky", EXIT_CRASH)],
        restart_delay=1.0,
        max_restart_delay=100.0,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    assert sup.tick() == []  # not due yet, but now scheduled
    first_due_in = sup._due_at["flaky"] - time.monotonic()
    assert 0.5 < first_due_in <= 1.0

    # Force it due now instead of sleeping out the delay, then let it
    # crash again immediately (EXIT_CRASH always crashes).
    sup._due_at["flaky"] = time.monotonic()
    assert sup.tick() == ["flaky"]
    _settle()

    assert sup.tick() == []
    second_due_in = sup._due_at["flaky"] - time.monotonic()
    assert 1.5 < second_due_in <= 2.0, "delay did not double from 1.0 to 2.0"


def test_restart_delay_is_capped(stopper):
    sup = Supervisor(
        [ChildSpec("flaky", EXIT_CRASH)],
        restart_delay=1.0,
        max_restart_delay=2.0,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    # Drive several consecutive crash/restart cycles; the delay would
    # otherwise keep doubling past the cap (1 -> 2 -> 4 -> 8 ...).
    for _ in range(4):
        sup.tick()
        sup._due_at["flaky"] = time.monotonic()
        sup.tick()
        _settle()

    sup.tick()
    due_in = sup._due_at["flaky"] - time.monotonic()
    assert due_in <= 2.0 + 0.1, "restart delay exceeded the configured cap"


def test_restart_delay_resets_after_the_child_stays_up(stopper):
    """A crash right after a healthy run must not inherit a stale backoff."""
    sup = Supervisor(
        [ChildSpec("flaky", CRASH_AFTER_BRIEF_UPTIME)],
        restart_delay=0.3,
        restart_reset_after=0.4,
    )
    stopper.append(sup)
    sup.start_all()
    time.sleep(0.8)  # let the first crash (at ~0.5s uptime) land

    assert sup.tick() == []
    first_due_in = sup._due_at["flaky"] - time.monotonic()
    assert first_due_in <= 0.3 + 0.1

    # Relaunch it now and let it run past restart_reset_after (0.4s)
    # before it crashes again (~0.5s uptime) -- backoff should reset to
    # the base delay rather than doubling to ~0.6s.
    sup._due_at["flaky"] = time.monotonic()
    sup.tick()
    time.sleep(0.8)

    sup.tick()
    second_due_in = sup._due_at["flaky"] - time.monotonic()
    assert second_due_in <= 0.3 + 0.1, "backoff did not reset after a healthy run"


def test_crash_looping_is_logged(stopper, capsys):
    sup = Supervisor(
        [ChildSpec("flaky", EXIT_CRASH)],
        restart_delay=0.1,
        max_restart_delay=0.2,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    for _ in range(4):
        sup.tick()
        sup._due_at["flaky"] = time.monotonic()
        sup.tick()
        _settle()

    out = capsys.readouterr().out
    assert "crash-looping" in out, out


# ---------------------------------------------------------------------------
# Keep-alive server
#
# Render only keeps a free web service awake while something answers HTTP on
# $PORT. That listener used to live inside bot.py's setup_hook, which runs
# only after a successful Discord login -- so an outage that stopped the bots
# connecting also silenced the listener, Render spun the service down, and
# every bot went with it. The supervisor owns it now precisely because the
# supervisor is the one thing that stays up when the children cannot.
# ---------------------------------------------------------------------------


def _get(port, path="/", timeout=5.0):
    """One HTTP GET against the keep-alive server, as an uptime pinger does."""
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read().decode()


def test_keepalive_answers_while_every_bot_is_down(stopper):
    """The regression test for the outage: no child alive, still answering.

    If this fails, Render sees a dead port, spins the service down, and the
    bots cannot come back even once Discord is reachable again.
    """
    sup = Supervisor([ChildSpec("crashy", EXIT_CRASH)], restart_delay=60.0)
    stopper.append(sup)
    server = supervisor_mod.start_keepalive(sup, port=0)
    assert server is not None, "keep-alive server did not bind"
    try:
        sup.start_all()
        _settle()
        sup.tick()
        assert sup.running_names() == [], "test needs every child dead"

        status, body = _get(server.server_address[1])
        assert status == 200
        assert body, "keep-alive answered with an empty body"
    finally:
        supervisor_mod.stop_keepalive(server)


def test_keepalive_reports_which_children_are_running(stopper):
    sup = Supervisor([ChildSpec("sleeper", SLEEP_FOREVER)])
    stopper.append(sup)
    server = supervisor_mod.start_keepalive(sup, port=0)
    try:
        sup.start_all()
        _wait_until(lambda: sup.running_names() == ["sleeper"])

        _status, body = _get(server.server_address[1])
        assert "sleeper" in body, body
    finally:
        supervisor_mod.stop_keepalive(server)


def test_keepalive_binds_the_port_env_var(stopper, monkeypatch):
    """Render injects $PORT; binding anything else means no open port."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    monkeypatch.setenv("PORT", str(free_port))
    sup = Supervisor([])
    stopper.append(sup)
    server = supervisor_mod.start_keepalive(sup)
    try:
        assert server is not None
        assert server.server_address[1] == free_port
    finally:
        supervisor_mod.stop_keepalive(server)


def test_keepalive_survives_a_port_already_in_use(stopper):
    """A taken port must not stop the bots -- they matter more than the ping."""
    import socket

    # Bound on 0.0.0.0, the same address the keep-alive server uses:
    # holding only 127.0.0.1 is not a conflict, because SO_REUSEADDR lets
    # a wildcard bind succeed alongside a loopback one.
    held = socket.socket()
    held.bind(("0.0.0.0", 0))
    held.listen(1)
    taken = held.getsockname()[1]
    try:
        sup = Supervisor([])
        stopper.append(sup)
        server = supervisor_mod.start_keepalive(sup, port=taken)
        assert server is None, "expected the bind to fail without raising"
    finally:
        held.close()


def test_run_binds_the_port_even_when_every_child_crashes():
    """End to end through run(), the entry point Render actually executes.

    Covers the wiring, not just start_keepalive: a keep-alive that works in
    isolation but is never started by run() would fail in exactly the way
    the outage did, and silently.
    """
    import socket
    import urllib.request

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    script = (
        "import supervisor\n"
        "supervisor.CHILDREN = [supervisor.ChildSpec('crashy', "
        f"[{sys.executable!r}, '-c', 'import sys; sys.exit(1)'])]\n"
        "supervisor.Supervisor(supervisor.CHILDREN, restart_delay=60.0).run()\n"
    )
    env = {**os.environ, "PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script], cwd=REPO_ROOT, env=env
    )
    try:
        def answers():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=1.0
                ) as r:
                    return r.status == 200
            except Exception:
                return False

        assert _wait_until(answers, timeout=15.0), (
            "run() never bound $PORT; Render would see a dead service and sleep it"
        )
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# -- Cloudflare IP ban (exit 75) ---------------------------------------------
#
# Discord rate-limits by IP, and all three bots share this instance's one
# egress IP. So the cooldown after a ban has to be service-wide: holding
# back only the child that reported it leaves its two siblings logging in
# every few seconds, refreshing the ban none of them can then get past.

EXIT_RATE_LIMITED_CHILD = _python(f"import sys; sys.exit({EXIT_RATE_LIMITED})")


def test_a_rate_limited_child_is_restarted_eventually():
    """75 means "come back later", not "stay stopped"."""
    assert should_restart(EXIT_RATE_LIMITED) is True
    for spec in CHILDREN:
        assert should_restart(EXIT_RATE_LIMITED, spec.no_restart_codes) is True


def test_a_rate_limited_child_waits_out_the_cooldown(stopper):
    sup = Supervisor(
        [ChildSpec("banned", EXIT_RATE_LIMITED_CHILD)],
        restart_delay=0.1,
        rate_limit_cooldown=30.0,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    assert sup.tick() == []
    # Its own short backoff has elapsed, but the cooldown has not.
    sup._due_at["banned"] = time.monotonic()
    assert sup.tick() == [], "relaunched during the rate-limit cooldown"


def test_the_cooldown_holds_back_the_siblings_too(stopper):
    """The ban is on the IP, so a sibling's login would refresh it."""
    sup = Supervisor(
        [
            ChildSpec("banned", EXIT_RATE_LIMITED_CHILD),
            ChildSpec("sibling", EXIT_CRASH),
        ],
        restart_delay=0.1,
        rate_limit_cooldown=30.0,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    assert sup.tick() == []
    sup._due_at["sibling"] = time.monotonic()
    assert sup.tick() == [], "a sibling relaunched during the cooldown"


def test_a_healthy_sibling_is_left_running_during_the_cooldown(stopper):
    """The ban bites on login. A bot already logged in is still working."""
    sup = Supervisor(
        [
            ChildSpec("banned", EXIT_RATE_LIMITED_CHILD),
            ChildSpec("healthy", SLEEP_FOREVER),
        ],
        restart_delay=0.1,
        rate_limit_cooldown=30.0,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    sup.tick()
    assert "healthy" in sup.running_names()


def test_children_restart_once_the_cooldown_expires(stopper):
    sup = Supervisor(
        [ChildSpec("banned", EXIT_RATE_LIMITED_CHILD)],
        restart_delay=0.1,
        rate_limit_cooldown=0.3,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    sup.tick()
    assert _wait_until(lambda: sup.tick() == ["banned"], timeout=3.0)


def test_the_cooldown_is_logged(stopper, capsys):
    sup = Supervisor(
        [ChildSpec("banned", EXIT_RATE_LIMITED_CHILD)],
        restart_delay=0.1,
        rate_limit_cooldown=30.0,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()
    sup.tick()

    out = capsys.readouterr().out
    assert "rate-limited" in out.lower()


def test_the_production_cooldown_outlasts_a_cloudflare_ban():
    """A 1015 lifts after the IP goes quiet -- minutes, not seconds."""
    assert RATE_LIMIT_COOLDOWN >= 900


# -- Self-ping (Render free-tier spin-down) ----------------------------------
#
# Render sleeps a free instance after ~15 minutes with no inbound request,
# and a sleeping instance fires no spawn notifications. An external pinger
# is the usual answer, but this service's hostname resolves to two edge
# addresses and one of them has a dead TLS listener, so roughly half of a
# monitor's checks hang and time out. Pinging our own public URL from
# inside removes the dependency on that monitor: traffic still arrives
# through Render's edge and still counts as activity, and here we can walk
# every resolved address instead of taking whichever one DNS hands over.

def test_the_self_ping_interval_beats_renders_idle_timeout():
    assert supervisor_mod.SELF_PING_INTERVAL < 15 * 60
    # Not so eager that a stalled request cannot finish before the next.
    assert supervisor_mod.SELF_PING_INTERVAL > supervisor_mod.SELF_PING_TIMEOUT


def test_ping_once_tries_the_first_address_that_answers():
    tried = []

    def fetch(address, *, host, timeout):
        tried.append(address)
        return 200

    assert supervisor_mod.ping_once(
        "https://example.onrender.com/",
        addresses=["1.1.1.1", "2.2.2.2"],
        fetch=fetch,
    )
    assert tried == ["1.1.1.1"], "a healthy address must not be followed by more"


def test_ping_once_falls_past_a_dead_address():
    """The whole point: one edge address never completes its handshake."""
    def fetch(address, *, host, timeout):
        if address == "216.24.57.15":
            raise TimeoutError("no TLS handshake")
        return 200

    assert supervisor_mod.ping_once(
        "https://example.onrender.com/",
        addresses=["216.24.57.15", "216.24.57.7"],
        fetch=fetch,
    )


def test_ping_once_is_false_when_every_address_fails():
    def fetch(address, *, host, timeout):
        raise OSError("unreachable")

    assert not supervisor_mod.ping_once(
        "https://example.onrender.com/",
        addresses=["1.1.1.1", "2.2.2.2"],
        fetch=fetch,
    )


def test_ping_once_rejects_an_error_status():
    """A 5xx means the edge answered but the service did not."""
    assert not supervisor_mod.ping_once(
        "https://example.onrender.com/",
        addresses=["1.1.1.1"],
        fetch=lambda address, *, host, timeout: 502,
    )


def test_ping_once_survives_a_resolution_failure():
    def resolve(host, port):
        raise OSError("DNS is down")

    assert not supervisor_mod.ping_once(
        "https://example.onrender.com/", resolve=resolve
    )


def test_ping_once_passes_the_hostname_for_sni():
    """Connecting by address still has to present the real hostname."""
    seen = {}

    def fetch(address, *, host, timeout):
        seen["host"] = host
        return 200

    supervisor_mod.ping_once(
        "https://bk-bot-3mk6.onrender.com/", addresses=["1.1.1.1"], fetch=fetch
    )
    assert seen["host"] == "bk-bot-3mk6.onrender.com"


def test_no_self_ping_without_a_public_url(monkeypatch):
    """Off the platform there is nothing to keep awake and no URL to hit."""
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert supervisor_mod.start_self_ping() is None


def test_the_self_ping_thread_is_a_daemon(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")
    thread = supervisor_mod.start_self_ping(ping=lambda url: True)
    try:
        assert thread is not None and thread.daemon, (
            "a non-daemon thread would keep the supervisor alive after shutdown"
        )
    finally:
        supervisor_mod.stop_self_ping(thread)


def test_a_successful_self_ping_is_visible(capsys):
    """A silent success cannot be told apart from a dead thread.

    The point of the self-ping is to prove the instance stayed awake, and
    that claim is unfalsifiable if only failures are ever printed.
    """
    supervisor_mod.report_ping(True, "https://example.onrender.com", 0.42)

    out = capsys.readouterr().out
    assert "self-ping" in out and "ok" in out
    assert "0.4" in out, "the round-trip time is the evidence it really left"


def test_a_failed_self_ping_says_so(capsys):
    supervisor_mod.report_ping(False, "https://example.onrender.com", 12.0)

    out = capsys.readouterr().out
    assert "self-ping" in out and "failed" in out


def test_the_first_self_ping_does_not_wait_for_the_interval():
    """A restart must not open an unguarded window.

    Waiting the full interval before the first ping left the instance
    unprotected for that whole period after every deploy -- which is
    exactly when it is most likely to be idle, since a deploy replaces
    the container and no visitor has arrived yet. Observed live: a deploy
    at 11:03 left the first ping due at 11:13, and the instance had
    already slept by 11:08.
    """
    pinged = threading.Event()
    thread = supervisor_mod.start_self_ping(
        "https://example.onrender.com",
        ping=lambda url: pinged.set() or True,
    )
    try:
        assert pinged.wait(timeout=5.0), "no ping before the first interval elapsed"
    finally:
        supervisor_mod.stop_self_ping(thread)


def test_a_single_missed_ping_cannot_reach_renders_idle_timeout():
    """Two intervals must still fit inside the 15-minute window.

    One ping can fail -- the dead edge address guarantees some will -- and
    the gap that leaves has to stay survivable rather than sleeping the
    instance until an external monitor happens to wake it.
    """
    assert supervisor_mod.SELF_PING_INTERVAL * 2 < 15 * 60


# -- Two rate limits, two cooldowns ------------------------------------------

EXIT_BRIEF_CHILD = _python(f"import sys; sys.exit({supervisor_mod.EXIT_RATE_LIMITED_BRIEF})")


def test_a_brief_rate_limit_is_restartable():
    assert should_restart(supervisor_mod.EXIT_RATE_LIMITED_BRIEF) is True
    for spec in CHILDREN:
        assert should_restart(
            supervisor_mod.EXIT_RATE_LIMITED_BRIEF, spec.no_restart_codes
        ) is True


def test_the_brief_cooldown_is_much_shorter_than_the_edge_ban_one():
    """Discord's global limit clears in minutes; a 1015 does not."""
    assert supervisor_mod.BRIEF_RATE_LIMIT_COOLDOWN < RATE_LIMIT_COOLDOWN
    assert supervisor_mod.BRIEF_RATE_LIMIT_COOLDOWN <= 600


def test_a_brief_rate_limit_uses_the_short_cooldown(stopper):
    sup = Supervisor(
        [ChildSpec("brief", EXIT_BRIEF_CHILD)],
        restart_delay=0.1,
        rate_limit_cooldown=30.0,
        brief_rate_limit_cooldown=0.3,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    sup.tick()
    # The long cooldown would still be holding it; the short one lets go.
    assert _wait_until(lambda: sup.tick() == ["brief"], timeout=3.0)


def test_an_edge_ban_still_uses_the_long_cooldown(stopper):
    sup = Supervisor(
        [ChildSpec("banned", EXIT_RATE_LIMITED_CHILD)],
        restart_delay=0.1,
        rate_limit_cooldown=30.0,
        brief_rate_limit_cooldown=0.3,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    sup.tick()
    sup._due_at["banned"] = time.monotonic()
    assert sup.tick() == [], "an edge ban must not get the short cooldown"


def test_the_brief_cooldown_also_covers_the_siblings(stopper):
    """The block is on the IP, so a sibling's login would extend it."""
    sup = Supervisor(
        [
            ChildSpec("brief", EXIT_BRIEF_CHILD),
            ChildSpec("sibling", EXIT_CRASH),
        ],
        restart_delay=0.1,
        rate_limit_cooldown=30.0,
        brief_rate_limit_cooldown=30.0,
    )
    stopper.append(sup)
    sup.start_all()
    _settle()

    sup.tick()
    sup._due_at["sibling"] = time.monotonic()
    assert sup.tick() == []
