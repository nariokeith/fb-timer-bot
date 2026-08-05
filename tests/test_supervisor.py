import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from supervisor import EXIT_NOT_CONFIGURED, ChildSpec, Supervisor, should_restart

REPO_ROOT = Path(__file__).resolve().parent.parent


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


SLEEP_FOREVER = _python("import time; time.sleep(300)")
EXIT_CRASH = _python("import sys; sys.exit(1)")
EXIT_CLEAN = _python("import sys; sys.exit(0)")
EXIT_UNCONFIGURED = _python(f"import sys; sys.exit({EXIT_NOT_CONFIGURED})")

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

        # ...and the restart isn't just deferred, it actually happens.
        assert _wait_until(
            lambda: len(_child_pids(harness.pid)) == 1,
            timeout=restart_delay + 3.0,
        )
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
