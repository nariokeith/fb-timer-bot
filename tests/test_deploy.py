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


# ---------------------------------------------------------------------------
# The service user cannot be hardcoded.
#
# Oracle's Ubuntu images create `ubuntu`. Google Cloud's do not -- they
# create an account named after the Google identity that first connects,
# so `User=ubuntu` is a unit that installs cleanly and then fails at boot
# on a box nobody is watching, which is the exact silent failure this
# file exists to catch.
# ---------------------------------------------------------------------------

USER_PLACEHOLDER = "__APP_USER__"


def test_the_unit_does_not_hardcode_a_user_the_host_may_not_have(unit):
    assert f"User={USER_PLACEHOLDER}" in unit
    assert "User=ubuntu" not in unit


def test_setup_fills_the_service_user_into_the_installed_unit(setup):
    """A placeholder nobody substitutes is worse than a wrong hardcoded name."""
    assert USER_PLACEHOLDER in setup


def test_setup_derives_the_service_user_from_whoever_ran_it(setup):
    """`sudo bash setup.sh` must land on the invoking account, not a guess."""
    app_user = re.search(r"^APP_USER=(.+)$", setup, re.MULTILINE).group(1)
    assert "SUDO_USER" in app_user, f"APP_USER={app_user} cannot adapt to the host"


# ---------------------------------------------------------------------------
# The Windows host runs the bots inside WSL2, not natively.
#
# bot.py:32 calls time.tzset(), which Python documents as Unix-only, so a
# native Windows run crashes the timer on import. WSL2 also means
# deploy/setup.sh and the systemd unit apply unchanged instead of needing
# a second, separately-rotting deployment path.
# ---------------------------------------------------------------------------

WINDOWS = REPO / "deploy" / "install-windows.ps1"
WINDOWS_DOC = REPO / "docs" / "running-on-windows.md"


@pytest.fixture
def windows():
    return WINDOWS.read_text()


def test_the_timer_still_depends_on_the_unix_only_call_that_forces_wsl():
    """If tzset ever goes, the WSL requirement should be revisited, not
    left as folklore in a guide nobody rereads."""
    assert "time.tzset()" in (REPO / "bot.py").read_text()


def test_the_windows_installer_starts_wsl_rather_than_a_bot(windows):
    """systemd inside the distro owns starting the supervisor; Windows
    only has to bring the distro up."""
    assert "wsl" in windows.lower()
    assert "supervisor.py" not in windows


def test_the_windows_installer_and_its_guide_agree_on_the_distro(windows):
    distro = re.search(r"\$Distro\s*=\s*['\"]([^'\"]+)['\"]", windows)
    assert distro, "install-windows.ps1 must name the distro in one place"
    assert distro.group(1) in WINDOWS_DOC.read_text()


def test_the_windows_installer_registers_an_autostart_task(windows):
    """A PC that reboots overnight must come back without a human."""
    assert "ScheduledTask" in windows


def test_the_windows_guide_warns_about_sleep(windows):
    """A sleeping host is the one failure mode WSL cannot paper over."""
    assert "sleep" in WINDOWS_DOC.read_text().lower()
