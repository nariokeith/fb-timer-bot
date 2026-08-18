"""The VPS deployment files must keep matching the code they start.

None of this runs in CI on a real VM, so the failure mode is silent: a
renamed entrypoint or a moved venv leaves a unit file that only fails at
3am on a box nobody is watching. These check the few facts the unit, the
setup script and the repo all have to agree on.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UNIT = REPO / "deploy" / "fb-timer-bot.service"
SETUP = REPO / "deploy" / "setup.sh"
UPDATE = REPO / "deploy" / "update.sh"


@pytest.fixture
def unit():
    return UNIT.read_text()


@pytest.fixture
def setup():
    return SETUP.read_text()


def _exec_start(unit_text: str) -> str:
    match = re.search(r"^ExecStart=(.+)$", unit_text, re.MULTILINE)
    assert match, "the unit has no ExecStart"
    return match.group(1)


def test_the_unit_starts_the_supervisor_not_a_single_bot(unit):
    """supervisor.py is the entrypoint; starting bot.py would run one of three."""
    assert _exec_start(unit).endswith("supervisor.py")
    assert (REPO / "supervisor.py").exists()


def test_the_unit_runs_python_unbuffered(unit):
    """Without -u, journald gets stdout in 8KB blocks instead of lines."""
    assert " -u " in _exec_start(unit)


def test_the_unit_and_setup_agree_on_where_the_app_lives(unit, setup):
    app_dir = re.search(r"^APP_DIR=(\S+)", setup, re.MULTILINE).group(1)
    assert f"WorkingDirectory={app_dir}" in unit
    assert _exec_start(unit).startswith(f"{app_dir}/.venv/bin/python")


def test_the_unit_and_setup_agree_on_the_service_user(unit, setup):
    app_user = re.search(r"^APP_USER=(\S+)", setup, re.MULTILINE).group(1)
    assert f"User={app_user}" in unit


def test_the_app_directory_stays_writable(unit):
    """ProtectSystem=full would otherwise make data.json read-only.

    bot.py writes data.json beside itself on every persist, so the
    hardening has to carve that directory back out.
    """
    app_dir = re.search(r"^WorkingDirectory=(\S+)", unit, re.MULTILINE).group(1)
    assert f"ReadWritePaths={app_dir}" in unit


def test_setup_installs_a_python_new_enough_for_the_pins(setup):
    """requirements.txt pins audioop-lts, which requires Python >= 3.13.

    Ubuntu 24.04 ships 3.12, so installing the distro python would fail on
    `pip install -r requirements.txt` -- after the VM is already built.
    """
    version = re.search(r"^PYTHON=python(3\.\d+)", setup, re.MULTILINE).group(1)
    assert tuple(int(part) for part in version.split(".")) >= (3, 13)


def test_setup_does_not_start_without_credentials(setup):
    """Starting credential-less would look like a crash, not a missing step."""
    assert "NOT started" in setup


def test_the_scripts_are_executable():
    for script in (SETUP, UPDATE):
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_the_scripts_fail_loudly():
    for script in (SETUP, UPDATE):
        assert "set -euo pipefail" in script.read_text(), script.name


def test_the_self_ping_stays_off_without_renders_url(monkeypatch):
    """A VPS never sleeps, so the self-ping should not run there at all.

    It keys off RENDER_EXTERNAL_URL, which only Render sets -- so this
    needs no deployment-specific configuration. Pinned here because it is
    the one behaviour that silently changes meaning off-platform.
    """
    import supervisor

    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert supervisor.start_self_ping() is None
