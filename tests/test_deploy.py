"""The macOS launchd agent must keep matching the code it starts.

None of this runs in CI against a real launchd, so the failure mode is
silent: a renamed entrypoint or a moved virtualenv leaves an agent that
only fails at 3am on a machine nobody is watching. These check the few
facts the plist, the installer and the repo all have to agree on.
"""

import plistlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLIST = REPO / "deploy" / "com.fbtimer.supervisor.plist"
INSTALLER = REPO / "deploy" / "install-macos.sh"


@pytest.fixture
def plist():
    return PLIST.read_text()


@pytest.fixture
def parsed():
    # __APP_DIR__ is substituted at install time; any absolute path will do
    # for checking the shape of the definition.
    return plistlib.loads(PLIST.read_text().replace("__APP_DIR__", "/tmp/app").encode())


def test_the_agent_is_valid_xml(parsed):
    """A malformed plist is rejected by launchctl with a useless error."""
    assert parsed["Label"] == "com.fbtimer.supervisor"


def test_the_agent_starts_the_supervisor_not_a_single_bot(parsed):
    """supervisor.py is the entrypoint; starting bot.py would run one of three."""
    argv = parsed["ProgramArguments"]
    assert argv[-1] == "supervisor.py"
    assert (REPO / "supervisor.py").exists()
    assert argv[-3].endswith("/.venv/bin/python")


def test_the_agent_runs_python_unbuffered(parsed):
    """Without -u the log arrives in 8KB blocks instead of lines."""
    assert "-u" in parsed["ProgramArguments"]


def test_the_agent_holds_off_idle_sleep(parsed):
    """A Mac that dozes off sends no spawn notifications."""
    assert parsed["ProgramArguments"][0] == "/usr/bin/caffeinate"


def test_the_agent_restarts_the_supervisor(parsed):
    assert parsed["KeepAlive"] is True
    assert parsed["ThrottleInterval"] >= 10, "a broken build would respawn in a loop"


def test_the_agent_runs_from_the_app_directory(parsed):
    """Relative paths -- supervisor.py, data.json, .env -- depend on it."""
    assert parsed["WorkingDirectory"] == "/tmp/app"


def test_the_installer_and_the_agent_agree_on_the_label(plist):
    label = re.search(r"^LABEL=(\S+)", INSTALLER.read_text(), re.MULTILINE).group(1)
    assert f"<string>{label}</string>" in plist


def test_the_installer_refuses_without_credentials():
    """Installing credential-less would look like a crash loop, not a step."""
    text = INSTALLER.read_text()
    assert ".env" in text and "exit 1" in text


def test_the_installer_can_uninstall():
    assert "--stop" in INSTALLER.read_text()


def test_the_installer_fails_loudly():
    assert "set -euo pipefail" in INSTALLER.read_text()


def test_the_installer_is_executable():
    assert INSTALLER.stat().st_mode & 0o111, "install-macos.sh is not executable"


def test_the_self_ping_stays_off_outside_render(monkeypatch):
    """A machine that never sleeps needs no self-ping, and gets none.

    It keys off RENDER_EXTERNAL_URL, which only Render sets, so running
    anywhere else needs no configuration change. Pinned here because it is
    the one behaviour that silently changes meaning off-platform.
    """
    import supervisor

    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert supervisor.start_self_ping() is None
