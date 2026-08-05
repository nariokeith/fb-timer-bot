import sys
import time

import pytest

from supervisor import EXIT_NOT_CONFIGURED, ChildSpec, Supervisor, should_restart


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


SLEEP_FOREVER = _python("import time; time.sleep(300)")
EXIT_CRASH = _python("import sys; sys.exit(1)")
EXIT_CLEAN = _python("import sys; sys.exit(0)")
EXIT_UNCONFIGURED = _python(f"import sys; sys.exit({EXIT_NOT_CONFIGURED})")


def _settle():
    """Give a short-lived child time to exit before polling."""
    time.sleep(0.4)


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
