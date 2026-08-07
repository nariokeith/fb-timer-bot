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
import time
from dataclasses import dataclass

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


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: list[str]
    no_restart_codes: frozenset[int] = NO_RESTART_CODES


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
    ):
        self._specs = {spec.name: spec for spec in specs}
        self._procs: dict[str, subprocess.Popen] = {}
        self._restart_delay = restart_delay
        self._max_restart_delay = max_restart_delay
        self._restart_reset_after = restart_reset_after
        self._stopping = False
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
