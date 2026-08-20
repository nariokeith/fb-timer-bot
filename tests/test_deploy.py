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
# Windows runs the bots natively, so a non-technical host can install
# them by running one file. WSL would have meant admin PowerShell, a
# reboot and a second run -- more than the person doing it can be asked
# to carry, and they cannot be talked through it remotely either.
# ---------------------------------------------------------------------------

WINDOWS = REPO / "deploy" / "install-windows.ps1"
WINDOWS_BAT = REPO / "deploy" / "INSTALL.bat"
WINDOWS_DOC = REPO / "docs" / "running-on-windows.md"


@pytest.fixture
def windows():
    return WINDOWS.read_text()


def test_tzset_is_guarded_because_windows_does_not_have_it():
    """Unguarded, bot.py crashes on import under native Windows -- which
    is now the supported way to run it there."""
    src = (REPO / "bot.py").read_text()
    assert 'hasattr(time, "tzset")' in src


def test_times_are_anchored_to_bot_tz_rather_than_the_process_zone():
    """The corollary: with no process-wide TZ on Windows, every naive
    <-> epoch conversion has to name the zone itself."""
    src = (REPO / "bot.py").read_text()
    assert "def _epoch(" in src
    assert "ZoneInfo" in src


def test_a_double_clickable_entry_point_exists():
    """Double-clicking a .ps1 opens Notepad; the host needs a .bat."""
    assert WINDOWS_BAT.exists()
    assert "install-windows.ps1" in WINDOWS_BAT.read_text()


def test_the_batch_wrapper_bypasses_the_execution_policy():
    """Default policy blocks unsigned scripts, and the failure message
    reads like a virus warning to anyone who has not seen it before."""
    assert "Bypass" in WINDOWS_BAT.read_text()


def test_the_installer_registers_an_autostart_task(windows):
    """A PC that reboots overnight must come back without a human."""
    assert "ScheduledTask" in windows


def test_the_installer_starts_the_supervisor_not_a_single_bot(windows):
    """supervisor.py owns the three bots; starting bot.py runs one."""
    assert "supervisor.py" in windows


def test_the_installer_writes_a_log_someone_can_send_back(windows):
    """Nobody here can see this machine, and the person running it cannot
    be asked to read a stack trace."""
    assert "Start-Transcript" in windows


def test_the_installer_refuses_without_credentials(windows):
    """A credential-less start exits 78 and the supervisor leaves the
    bots stopped, which reads as a crash rather than a missing step."""
    assert ".env" in windows


def test_the_guide_warns_about_sleep():
    """A sleeping host is the one failure mode nothing here can paper over."""
    assert "sleep" in WINDOWS_DOC.read_text().lower()


def _required_credentials() -> set[str]:
    """What the three bots actually refuse to start without.

    Read out of the bots rather than restated here, so adding a required
    variable to one of them fails in this file instead of at 3am on a PC
    nobody can see.
    """
    attendance = (REPO / "attendance_bot.py").read_text()
    block = attendance[
        attendance.index("    missing = [") : attendance.index("        if not value")
    ]
    names = set(re.findall(r'\("([A-Z_]+)",', block))

    items = (REPO / "items_bot.py").read_text()
    declared = re.search(r"REQUIRED_ENV = \(([^)]*)\)", items).group(1)
    names |= {n.strip().strip('"') for n in declared.split(",") if n.strip()}

    names.add("DISCORD_TOKEN")  # bot.py, checked inline at its entry point
    return names


def test_the_installer_checks_every_credential_the_bots_require(windows):
    """A missing one means exit 78 and three bots left stopped, which the
    person who ran the installer would see as 'it just did nothing'."""
    missing = sorted(n for n in _required_credentials() if n not in windows)

    assert not missing, f"installer never validates: {missing}"


def test_the_installer_rejects_a_multiline_service_account_key(windows):
    """A pasted key spanning lines is not valid JSON and fails at runtime
    with a message nobody on that PC can act on. It cost an hour once."""
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in windows
    assert "ConvertFrom-Json" in windows


def test_the_installer_never_slices_an_argument_array_by_range(windows):
    """`$a[1..($a.Count-1)]` looks like "everything after the first" and
    is not: for a one-element array 1..0 counts DOWN, so it yields $null
    and then the element itself. That turned `python --version` into
    `python python --version` and made Python detection fail silently.
    """
    code = "\n".join(
        line for line in windows.splitlines() if not line.strip().startswith("#")
    )
    assert ".Count-1)]" not in code.replace(" ", "")


def test_requirements_pin_the_timezone_database():
    """Windows has no system IANA database, so this is what makes
    ZoneInfo("Asia/Manila") resolve there instead of raising on import."""
    assert "tzdata" in (REPO / "requirements.txt").read_text()


def test_the_installer_brings_its_own_python(windows):
    """Nothing may need installing on the host.

    Installing Python system-wide was the biggest failure point: it wants
    winget or a downloaded installer, changes PATH, and can need a reboot
    before the new PATH is visible -- none of which someone can be walked
    through blind. The embeddable distribution is a zip that runs where it
    is unpacked.
    """
    assert "embed-amd64.zip" in windows


def test_the_installer_enables_site_in_the_embedded_python(windows):
    """The embeddable build ships python313._pth with `import site`
    commented out, and pip cannot install anything until it is on."""
    assert "_pth" in windows
    assert "import site" in windows


def test_the_installer_needs_no_administrator(windows):
    """Everything lands under the user's own profile, so no UAC prompt
    appears -- one more dialog nobody could explain over Discord."""
    assert "LOCALAPPDATA" in windows
    code = "\n".join(
        line for line in windows.splitlines() if not line.strip().startswith("#")
    )
    for elevated in ("winget", "Start-Process -Verb RunAs", "InstallAllUsers"):
        assert elevated not in code, f"{elevated!r} needs elevation or a system install"


# ---------------------------------------------------------------------------
# Updating is re-running INSTALL.bat, so a re-run has to be cheap and
# non-destructive. The first version was neither: it deleted the embedded
# Python and every wheel and fetched them again -- about 40 MB and several
# minutes for a one-line change -- and took the log directory with it,
# which lives inside the code directory it wiped.
# ---------------------------------------------------------------------------


def test_an_update_keeps_the_existing_python(windows):
    """Re-downloading 10 MB of interpreter to change a line of Python is
    minutes of someone else's time, and another chance to fail."""
    assert "Reusing the Python already installed" in windows


def test_an_update_preserves_the_logs(windows):
    """DEFAULT_LOG_FILE sits inside the code directory, so refreshing the
    code deletes the history -- exactly when an update is being made
    because something is wrong."""
    assert "logs" in windows
    assert "Move-Item" in windows


def test_an_update_does_not_need_the_credentials_sent_again(windows):
    """Otherwise every update means re-sending live tokens over chat, and
    keeping a folder full of them on someone else's desktop forever."""
    assert "already installed" in windows


def test_the_bots_run_without_a_console_window(windows):
    """A scheduled task with an at-logon trigger runs in the user's own
    session, so a console executable puts a black window on their desktop
    at every logon -- forever, saying nothing, in front of someone who has
    every reason to close it. Closing it kills all three bots.

    pythonw.exe is the same interpreter without the console. It ships in
    the embeddable distribution, and it costs nothing here because the
    output that matters goes to the log file: print() is a documented
    no-op when sys.stdout is None, and the children's output is read
    through pipes the supervisor owns rather than through its stdout.
    """
    action = re.search(r"New-ScheduledTaskAction[^\n]*\n[^\n]*", windows).group(0)
    assert "pythonw" in windows
    assert "$pythonw" in action, f"the task still launches a console python: {action}"
