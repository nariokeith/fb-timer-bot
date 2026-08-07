# Screenshot Attendance Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A second Discord bot lets an officer post `!attendance <boss>` with an in-game roster screenshot, adding that boss's point value to each matched player's row in the *Point System* Google Sheet — without the production timer bot being modified, imported, or sharing a process.

**Architecture:** `supervisor.py` becomes the Render start command. It launches two independent subprocesses: `python -u bot.py` (untouched) and `python -u attendance_bot.py` (a separate Discord application with its own token). Either can crash and restart without affecting the other. Boss names resolve against the sheet's own header row, so there is no boss table to keep in sync with the timer.

**Tech Stack:** Python 3.14 · discord.py 2.7.1 (already present) · `google-genai` (Gemini Interactions API, free tier) · `gspread` + `google-auth` · pytest.

## Global Constraints

- **`bot.py` must never be modified.** Verify with `git diff --exit-code bot.py` before every commit. It is also never imported by any new module.
- **The timer must survive anything the attendance bot does.** Every design choice defers to this.
- Python 3.14.5, run via `.venv/bin/python`.
- Only `bot.py` binds `$PORT`. The attendance bot has no web server.
- The Gemini free tier is the only permitted vision path. No paid API calls.
- Column B (`Points`) in the sheet is a SUM formula and must never be written.
- Every blocking network call (gspread, Gemini) runs inside `asyncio.to_thread(...)`.
- Commit messages match this repo's existing style — a plain descriptive sentence, no `feat:`/`fix:` prefix — and end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Bosses worth 3 points, verbatim: `Lucus`, `Libitina`, `Rakajeth`, `Icaruthia`, `Motti`, `Nevaeh`, `Tumier`, `Camalia`. Every other boss is worth 1.

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `bot.py` | Timer bot. **Untouched, never imported.** | existing |
| `supervisor.py` | Start and restart the two bot processes | create |
| `attendance_bot.py` | Discord commands, permission gate, preview/confirm | create |
| `attendance_bosses.py` | Point values; resolve a boss name against sheet headers | create |
| `attendance_roster.py` | Name normalization and fuzzy matching. Pure. | create |
| `attendance_vision.py` | Gemini call: image bytes → name strings | create |
| `attendance_sheet.py` | gspread: players, cells, batched writes, config, audit log | create |
| `tests/conftest.py` | Shared fakes | create |
| `tests/test_supervisor.py` | | create |
| `tests/test_attendance_bosses.py` | | create |
| `tests/test_attendance_roster.py` | | create |
| `tests/test_attendance_vision.py` | | create |
| `tests/test_attendance_sheet.py` | | create |
| `requirements.txt` | add three runtime deps | modify |
| `requirements-dev.txt` | pytest only — never installed on Render | create |
| `render.yaml` | start command → `supervisor.py`; four new env vars | modify |
| `README.md` | attendance section + setup | modify |

---

### Task 1: Process supervisor

Built first because it is the safety property everything else depends on. Ships useful on its own: after this task the timer runs under supervision and is *more* resilient than today, with no attendance code in the picture.

**Files:**
- Create: `supervisor.py`
- Create: `tests/test_supervisor.py`
- Create: `requirements-dev.txt`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ChildSpec` — frozen dataclass, fields `name: str`, `argv: list[str]`
  - `Supervisor(specs: list[ChildSpec], *, restart_delay: float = 5.0)` with methods `start_all() -> None`, `tick() -> list[str]` (names restarted this tick), `stop_all(timeout: float = 10.0) -> None`, `run() -> None`, `running_names() -> list[str]`, `pid_of(name) -> int | None`
  - `should_restart(exit_code: int) -> bool`
  - `NO_RESTART_CODES: frozenset[int]`, `EXIT_NOT_CONFIGURED = 78`

- [ ] **Step 1: Create the working branch**

The repo is on `main`. Never commit this work there.

```bash
cd "/Users/keithjustinnario/Documents/FB TIMER M2"
git checkout -b attendance-logging
```

- [ ] **Step 2: Install pytest as a dev-only dependency**

pytest must NOT go in `requirements.txt` — Render installs that file, and every extra package costs cold-start time on the free tier.

Create `requirements-dev.txt`:

```
pytest==8.3.4
```

```bash
.venv/bin/pip install -r requirements-dev.txt
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_supervisor.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_supervisor.py -v
```

Expected: `ModuleNotFoundError: No module named 'supervisor'`

- [ ] **Step 5: Write the implementation**

Create `supervisor.py`:

```python
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
EXIT_NOT_CONFIGURED = 78
NO_RESTART_CODES = frozenset({0, EXIT_NOT_CONFIGURED})

POLL_INTERVAL = 2.0


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: list[str]


def should_restart(exit_code: int) -> bool:
    """True if a child that exited with this code should be relaunched."""
    return exit_code not in NO_RESTART_CODES


class Supervisor:
    """Starts child processes and relaunches the ones that crash."""

    def __init__(self, specs: list[ChildSpec], *, restart_delay: float = 5.0):
        self._specs = {spec.name: spec for spec in specs}
        self._procs: dict[str, subprocess.Popen] = {}
        self._restart_delay = restart_delay
        self._stopping = False

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

    def start_all(self) -> None:
        for name in self._specs:
            self._launch(name)

    def tick(self) -> list[str]:
        """Check every child once; relaunch the ones that crashed.

        Returns the names restarted, so callers and tests can see what
        happened without parsing logs.
        """
        restarted = []
        for name, proc in list(self._procs.items()):
            code = proc.poll()
            if code is None:
                continue

            if not should_restart(code):
                print(
                    f"[supervisor] {name} exited with {code}; leaving it stopped",
                    flush=True,
                )
                del self._procs[name]
                continue

            print(
                f"[supervisor] {name} exited with {code}; restarting in "
                f"{self._restart_delay}s",
                flush=True,
            )
            if self._restart_delay:
                time.sleep(self._restart_delay)
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

    def run(self) -> None:
        """Start everything and supervise until told to stop."""

        def handle_signal(signum, _frame):
            print(f"[supervisor] received signal {signum}", flush=True)
            self.stop_all()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        self.start_all()
        while not self._stopping:
            time.sleep(POLL_INTERVAL)
            self.tick()
            if not self._procs:
                print("[supervisor] no children left; exiting", flush=True)
                return


CHILDREN = [
    ChildSpec("timer", [sys.executable, "-u", "bot.py"]),
    ChildSpec("attendance", [sys.executable, "-u", "attendance_bot.py"]),
]


if __name__ == "__main__":
    Supervisor(CHILDREN).run()
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_supervisor.py -v
```

Expected: 9 passed.

- [ ] **Step 7: Verify the timer still starts under supervision**

`attendance_bot.py` does not exist yet, so its child fails — which is exactly the scenario to prove safe. The timer must come up regardless.

```bash
cd "/Users/keithjustinnario/Documents/FB TIMER M2"
timeout 20 .venv/bin/python supervisor.py 2>&1 | head -30
```

Expected: `[supervisor] starting timer`, `[supervisor] starting attendance`, the attendance child failing with a `No such file` error and being restarted, and the timer logging `Logged in as ...` plus `Keep-alive server listening on port 8080.` **The timer must reach "Logged in" despite the attendance child failing.** If it does not, stop and fix the supervisor before continuing.

- [ ] **Step 8: Confirm bot.py is untouched, then commit**

```bash
git diff --exit-code bot.py && echo "bot.py clean"
git add supervisor.py tests/test_supervisor.py requirements-dev.txt
git commit -m "$(cat <<'EOF'
Run the bots as separate supervised processes

The attendance feature is going in as a second Discord bot rather than
new commands on the timer, so a crash, a bad deploy or a blocked event
loop on the attendance side cannot take the timer down. The supervisor
starts both as subprocesses and restarts whichever one dies.

A child that exits 0 or 78 is left stopped rather than restarted, so
deploying before the attendance credentials exist produces one log line
instead of a crash loop -- and the timer still runs.

Two free Render services would isolate them further, but the 750 free
hours are shared per workspace: two 24/7 services exhaust them mid-month
and Render then suspends every free service, timer included.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Point values and boss column resolution

**Files:**
- Create: `attendance_bosses.py`
- Create: `tests/test_attendance_bosses.py`

**Interfaces:**
- Consumes: nothing. **Must not import `bot.py`.**
- Produces:
  - `boss_points(boss: str) -> int`
  - `header_base(header: str) -> str`
  - `resolve_boss(headers: list[str], query: str) -> str` — returns the header base name
  - `BossNotFound(ValueError)`, `BossAmbiguous(ValueError)`
  - `BOSSES_WORTH_3: frozenset[str]`, `NON_BOSS_HEADERS: frozenset[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_attendance_bosses.py`:

```python
import pytest

from attendance_bosses import (
    BOSSES_WORTH_3,
    BossAmbiguous,
    BossNotFound,
    boss_points,
    header_base,
    resolve_boss,
)

HEADERS = [
    "Player Name", "Points", "Lucus - 3", "EGO", "Clemantis", "Livera",
    "Araneo", "Undomiel", "Saphirus", "Neutro", "Lady Dalia",
    "General Aqueus", "Thymele", "Amentis", "Baron Braudmor", "Motti - 3",
]


def test_three_point_bosses_are_worth_three():
    for boss in ["Lucus", "Libitina", "Rakajeth", "Icaruthia",
                 "Motti", "Nevaeh", "Tumier", "Camalia"]:
        assert boss_points(boss) == 3


def test_every_other_boss_is_worth_one():
    for boss in ["EGO", "Livera", "Lady Dalia", "Clemantis", "Amentis"]:
        assert boss_points(boss) == 1


def test_point_lookup_ignores_case():
    assert boss_points("lucus") == 3
    assert boss_points("LIVERA") == 1


def test_header_base_strips_the_point_annotation():
    assert header_base("Lucus - 3") == "Lucus"
    assert header_base("  EGO  ") == "EGO"
    assert header_base("Lady Dalia") == "Lady Dalia"


def test_resolves_an_exact_header():
    assert resolve_boss(HEADERS, "Livera") == "Livera"
    assert resolve_boss(HEADERS, "Lady Dalia") == "Lady Dalia"


def test_resolution_ignores_case():
    assert resolve_boss(HEADERS, "livera") == "Livera"
    assert resolve_boss(HEADERS, "eGo") == "EGO"


def test_resolves_an_annotated_header_by_its_base_name():
    assert resolve_boss(HEADERS, "Lucus") == "Lucus"
    assert resolve_boss(HEADERS, "lucus") == "Lucus"


def test_resolves_a_unique_prefix():
    assert resolve_boss(HEADERS, "undo") == "Undomiel"
    assert resolve_boss(HEADERS, "gen") == "General Aqueus"


def test_prefix_matching_is_on_the_full_header_name():
    # "dal" is not a prefix of "Lady Dalia", so it must not match.
    with pytest.raises(BossNotFound):
        resolve_boss(HEADERS, "dal")


def test_ambiguous_prefix_names_the_candidates():
    with pytest.raises(BossAmbiguous) as excinfo:
        resolve_boss(["Player Name", "Points", "Motti - 3", "Mother"], "mot")
    assert "Motti" in str(excinfo.value)
    assert "Mother" in str(excinfo.value)


def test_unknown_boss_raises():
    with pytest.raises(BossNotFound):
        resolve_boss(HEADERS, "Godzilla")


def test_structural_columns_are_never_treated_as_bosses():
    with pytest.raises(BossNotFound):
        resolve_boss(HEADERS, "Points")
    with pytest.raises(BossNotFound):
        resolve_boss(HEADERS, "Player Name")


def test_blank_headers_are_ignored():
    assert resolve_boss(["Player Name", "Points", "", "  ", "EGO"], "ego") == "EGO"


def test_three_pointer_names_are_stored_without_stray_whitespace():
    assert BOSSES_WORTH_3 == {b.strip() for b in BOSSES_WORTH_3}
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_attendance_bosses.py -v
```

Expected: `ModuleNotFoundError: No module named 'attendance_bosses'`

- [ ] **Step 3: Write the implementation**

Create `attendance_bosses.py`:

```python
"""Point values, and resolving a typed boss name against the sheet's headers.

The sheet's header row is the source of truth for which bosses have an
attendance column, so there is no boss table to keep in sync with the
timer -- and this module deliberately does not import bot.py. Adding a
boss column to the sheet makes it loggable with no code change.
"""

# Confirmed by the guild owner on 2026-08-06. Everything else is worth 1.
BOSSES_WORTH_3 = frozenset({
    "Lucus", "Libitina", "Rakajeth", "Icaruthia",
    "Motti", "Nevaeh", "Tumier", "Camalia",
})

# Columns that exist in the sheet but are not bosses.
NON_BOSS_HEADERS = frozenset({"player name", "points"})

_POINTS_INDEX = {name.casefold(): 3 for name in BOSSES_WORTH_3}


class BossNotFound(ValueError):
    """No column in the sheet matches this name."""


class BossAmbiguous(ValueError):
    """More than one column matches this name."""


def header_base(header: str) -> str:
    """The boss name inside a header cell.

    Some headers annotate their point value, e.g. "Lucus - 3".
    """
    return header.split(" - ")[0].strip()


def boss_points(boss: str) -> int:
    """Points awarded for one attendance at this boss."""
    return _POINTS_INDEX.get(boss.strip().casefold(), 1)


def _boss_headers(headers: list[str]) -> list[str]:
    names = []
    for cell in headers:
        base = header_base(cell)
        if base and base.casefold() not in NON_BOSS_HEADERS:
            names.append(base)
    return names


def resolve_boss(headers: list[str], query: str) -> str:
    """Match a typed name against the sheet's boss columns.

    Case-insensitive exact match first, then unique prefix -- the same
    convention bot.py uses for its own commands. Never guesses: an input
    matching nothing, or several columns, raises.
    """
    wanted = query.strip().casefold()
    if not wanted:
        raise BossNotFound("No boss name given")

    names = _boss_headers(headers)

    for name in names:
        if name.casefold() == wanted:
            return name

    matches = [name for name in names if name.casefold().startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise BossAmbiguous(
            f"{query!r} matches several columns: {', '.join(sorted(matches))}"
        )
    raise BossNotFound(f"No column matches {query!r}")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_attendance_bosses.py -v
```

Expected: 14 passed.

- [ ] **Step 5: Confirm bot.py is untouched, then commit**

```bash
git diff --exit-code bot.py && echo "bot.py clean"
git add attendance_bosses.py tests/test_attendance_bosses.py
git commit -m "$(cat <<'EOF'
Resolve boss names against the sheet's own header row

The attendance sheet spells several bosses differently from the timer
(Undomiel, Lady Dalia, General Aqueus, Baron Braudmor), and it is the
sheet that decides which bosses have a column at all. Matching against
its header row removes the mapping table entirely and means adding a
column makes that boss loggable with no code change.

Eight bosses are worth 3 points per attendance; the rest are worth 1.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Player name matching

Pure functions, no network. This module is why a free OCR engine is sufficient: it matches against a closed set of roughly 35 known names rather than trusting open-ended transcription.

**Files:**
- Create: `attendance_roster.py`
- Create: `tests/test_attendance_roster.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `normalize(name: str) -> str`
  - `Match` — frozen dataclass, fields `raw: str`, `player: str`, `score: float`
  - `match_names(raw_names: list[str], known_players: list[str]) -> tuple[list[Match], list[str]]` returning `(matched, unmatched_raw_names)`, deduplicated by resolved player
  - `MATCH_THRESHOLD: float`, `AMBIGUITY_MARGIN: float`

- [ ] **Step 1: Write the failing test**

Create `tests/test_attendance_roster.py`:

```python
from attendance_roster import Match, match_names, normalize

KNOWN = [
    "ARCILynN", "xSigarilyas", "Talong", "XxLINGAxX", "fLuffy", "Kobe",
    "ToastedBread", "wileKAMOTE卐", "BudoySul (Riuz)", "chinchong ni Mumu",
]


def _players(matched):
    return sorted(m.player for m in matched)


def test_exact_names_match():
    matched, unmatched = match_names(["Kobe", "Talong"], KNOWN)
    assert _players(matched) == ["Kobe", "Talong"]
    assert unmatched == []


def test_matching_ignores_case_and_surrounding_whitespace():
    matched, unmatched = match_names(["  kobe  ", "TALONG"], KNOWN)
    assert _players(matched) == ["Kobe", "Talong"]
    assert unmatched == []


def test_non_ascii_names_survive_normalization():
    matched, unmatched = match_names(["wileKAMOTE卐"], KNOWN)
    assert _players(matched) == ["wileKAMOTE卐"]
    assert unmatched == []


def test_names_with_parentheses_and_spaces_match():
    matched, unmatched = match_names(
        ["BudoySul (Riuz)", "chinchong  ni  Mumu"], KNOWN
    )
    assert _players(matched) == ["BudoySul (Riuz)", "chinchong ni Mumu"]
    assert unmatched == []


def test_single_character_ocr_error_still_matches():
    # 'l' misread for 'i' -- scores 0.909, above the 0.85 threshold.
    matched, unmatched = match_names(["xSigarllyas"], KNOWN)
    assert _players(matched) == ["xSigarilyas"]
    assert unmatched == []


def test_unknown_name_is_reported_not_guessed():
    matched, unmatched = match_names(["TotallyNewGuy"], KNOWN)
    assert matched == []
    assert unmatched == ["TotallyNewGuy"]


def test_ambiguous_name_is_reported_not_guessed():
    # Both candidates score 0.909 -- a tie inside the margin must not
    # silently award points to whichever happens to sort first.
    matched, unmatched = match_names(["Kobe0"], ["Kobe01", "Kobe02"])
    assert matched == []
    assert unmatched == ["Kobe0"]


def test_same_player_read_twice_is_only_awarded_once():
    matched, unmatched = match_names(["Kobe", "kobe", "  KOBE"], KNOWN)
    assert _players(matched) == ["Kobe"]
    assert unmatched == []


def test_blank_and_whitespace_only_names_are_discarded():
    matched, unmatched = match_names(["", "   ", "Kobe"], KNOWN)
    assert _players(matched) == ["Kobe"]
    assert unmatched == []


def test_match_carries_the_raw_text_that_produced_it():
    matched, _ = match_names(["xSigarllyas"], KNOWN)
    assert matched[0] == Match(
        raw="xSigarllyas", player="xSigarilyas", score=matched[0].score
    )
    assert matched[0].score >= 0.85


def test_normalize_collapses_case_and_internal_whitespace():
    assert normalize("  Chinchong   NI  mumu ") == "chinchong ni mumu"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_attendance_roster.py -v
```

Expected: `ModuleNotFoundError: No module named 'attendance_roster'`

- [ ] **Step 3: Write the implementation**

Create `attendance_roster.py`:

```python
"""Match names read off a screenshot against the players already in the sheet.

The bot never invents a player row. Every raw string either resolves to a
name already in column A, or it is reported as unmatched for a human to
sort out. That constraint is what lets a free OCR engine be good enough --
this is matching against ~35 known strings, not open transcription.
"""

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# A single-character misread in an 11-character name scores about 0.909,
# so 0.85 accepts realistic OCR noise. Unrelated names top out near 0.53.
MATCH_THRESHOLD = 0.85

# The best candidate must beat the runner-up by this much. Without it,
# "Kobe0" against "Kobe01" and "Kobe02" (both 0.909) would award points
# to whichever happened to sort first.
AMBIGUITY_MARGIN = 0.05


@dataclass(frozen=True)
class Match:
    raw: str
    player: str
    score: float


def normalize(name: str) -> str:
    """Casefold and collapse whitespace, preserving non-ASCII characters.

    NFKC rather than stripping to ASCII, because real player names contain
    characters like the one in "wileKAMOTE卐".
    """
    folded = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(folded.split())


def _score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _best_candidate(target: str, index: dict[str, str]) -> tuple[str, float] | None:
    """Highest-scoring player for `target`, or None if unclear."""
    scored = sorted(
        ((_score(target, key), player) for key, player in index.items()),
        reverse=True,
    )
    if not scored:
        return None

    best_score, best_player = scored[0]
    if best_score < MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and best_score - scored[1][0] < AMBIGUITY_MARGIN:
        return None
    return best_player, best_score


def match_names(
    raw_names: list[str], known_players: list[str]
) -> tuple[list[Match], list[str]]:
    """Resolve raw strings to known players.

    Returns (matched, unmatched). Matches are deduplicated by resolved
    player, so a name the model reads twice never earns double points.
    """
    index = {normalize(p): p for p in known_players if p.strip()}

    matched: list[Match] = []
    unmatched: list[str] = []
    seen: set[str] = set()

    for raw in raw_names:
        target = normalize(raw)
        if not target:
            continue

        if target in index:
            player, score = index[target], 1.0
        else:
            candidate = _best_candidate(target, index)
            if candidate is None:
                unmatched.append(raw)
                continue
            player, score = candidate

        if player in seen:
            continue
        seen.add(player)
        matched.append(Match(raw=raw, player=player, score=score))

    return matched, unmatched
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_attendance_roster.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Confirm bot.py is untouched, then commit**

```bash
git diff --exit-code bot.py && echo "bot.py clean"
git add attendance_roster.py tests/test_attendance_roster.py
git commit -m "$(cat <<'EOF'
Match screenshot names against the players already in the sheet

Matching runs exact, then case- and whitespace-insensitive, then fuzzy
with a 0.85 floor. A name with no clear winner, or a tie between two
close candidates, is reported as unmatched rather than guessed -- a wrong
guess silently awards points to the wrong person. Duplicates collapse so
a name read twice off one screenshot is only paid once.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Gemini vision extraction

**Files:**
- Create: `attendance_vision.py`
- Create: `tests/conftest.py`
- Create: `tests/test_attendance_vision.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `extract_names(image_bytes: bytes, mime_type: str, *, client=None) -> list[str]`
  - `VisionError(RuntimeError)`
  - `MODEL: str`, `PROMPT: str`, `RESPONSE_SCHEMA: dict`

**API note — read before writing code.** The current Gemini Python SDK uses the **Interactions API**: `client.interactions.create(...)` returning an object with `.output_text`. It is *not* `client.models.generate_content(...)`, which is the older shape you may recall. The structured-output parameter is `response_format`, not `response_schema`.

- [ ] **Step 1: Add the runtime dependencies**

Append to `requirements.txt`:

```
google-genai==1.44.0
gspread==6.2.1
google-auth==2.40.3
```

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/pip freeze | grep -iE "^(google-genai|gspread|google-auth)="
```

If the resolved versions differ from the pins above, update `requirements.txt` to match exactly what `pip freeze` reports, so Render installs the same thing.

- [ ] **Step 2: Write the shared test fake**

Create `tests/conftest.py`:

```python
"""Fakes shared across the attendance test modules."""

from dataclasses import dataclass, field


@dataclass
class FakeInteraction:
    output_text: str


@dataclass
class _FakeInteractions:
    output_text: str = ""
    error: Exception | None = None
    calls: list[dict] = field(default_factory=list)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FakeInteraction(output_text=self.output_text)


@dataclass
class FakeGeminiClient:
    """Stands in for google.genai.Client in tests."""

    output_text: str = ""
    error: Exception | None = None

    def __post_init__(self):
        self.interactions = _FakeInteractions(
            output_text=self.output_text, error=self.error
        )

    @property
    def calls(self):
        return self.interactions.calls
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_attendance_vision.py`:

```python
import base64
import json

import pytest

from attendance_vision import MODEL, VisionError, extract_names
from conftest import FakeGeminiClient

IMAGE = b"\x89PNG\r\n\x1a\n fake image bytes"


def test_returns_the_names_the_model_reported():
    client = FakeGeminiClient(
        output_text=json.dumps({"names": ["Kobe", "Talong", "fLuffy"]})
    )
    assert extract_names(IMAGE, "image/png", client=client) == [
        "Kobe", "Talong", "fLuffy",
    ]


def test_sends_the_image_base64_encoded_with_its_mime_type():
    client = FakeGeminiClient(output_text=json.dumps({"names": ["Kobe"]}))
    extract_names(IMAGE, "image/png", client=client)

    call = client.calls[0]
    assert call["model"] == MODEL

    image_part = next(p for p in call["input"] if p["type"] == "image")
    assert image_part["mime_type"] == "image/png"
    assert base64.b64decode(image_part["data"]) == IMAGE


def test_requests_json_constrained_to_the_schema():
    client = FakeGeminiClient(output_text=json.dumps({"names": ["Kobe"]}))
    extract_names(IMAGE, "image/png", client=client)

    fmt = client.calls[0]["response_format"]
    assert fmt["mime_type"] == "application/json"
    assert fmt["schema"]["properties"]["names"]["type"] == "array"


def test_empty_result_is_an_error_not_a_silent_no_op():
    client = FakeGeminiClient(output_text=json.dumps({"names": []}))
    with pytest.raises(VisionError, match="no names"):
        extract_names(IMAGE, "image/png", client=client)


def test_unparseable_response_raises():
    client = FakeGeminiClient(output_text="I'm sorry, I can't read that image.")
    with pytest.raises(VisionError, match="not valid JSON"):
        extract_names(IMAGE, "image/png", client=client)


def test_response_missing_the_names_key_raises():
    client = FakeGeminiClient(output_text=json.dumps({"players": ["Kobe"]}))
    with pytest.raises(VisionError, match="unexpected shape"):
        extract_names(IMAGE, "image/png", client=client)


def test_non_string_entries_are_rejected():
    client = FakeGeminiClient(output_text=json.dumps({"names": ["Kobe", 42]}))
    with pytest.raises(VisionError, match="unexpected shape"):
        extract_names(IMAGE, "image/png", client=client)


def test_api_failure_is_wrapped_in_vision_error():
    client = FakeGeminiClient(error=RuntimeError("429 quota exceeded"))
    with pytest.raises(VisionError, match="quota exceeded"):
        extract_names(IMAGE, "image/png", client=client)


def test_blank_names_are_dropped():
    client = FakeGeminiClient(
        output_text=json.dumps({"names": ["Kobe", "  ", "", "Talong"]})
    )
    assert extract_names(IMAGE, "image/png", client=client) == ["Kobe", "Talong"]
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_attendance_vision.py -v
```

Expected: `ModuleNotFoundError: No module named 'attendance_vision'`

- [ ] **Step 5: Write the implementation**

Create `attendance_vision.py`:

```python
"""Read player names off a roster screenshot using the Gemini free tier.

Uses the Interactions API (client.interactions.create), the current Gemini
SDK surface -- not the older models.generate_content. The reply is
constrained by a JSON schema so the model cannot answer with prose that
would need parsing.
"""

import base64
import json
import os

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "names": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["names"],
}

# Validated against a real Manage Rally screenshot from this guild on
# 2026-08-06: 4/4 runs correctly excluded the one dimmed player, and 3/3
# runs on a crop containing no dimmed players kept all ten -- so it does
# not over-exclude. The earlier, looser wording included the dimmed
# player 4/4 times.
PROMPT = (
    "This image is a roster panel from the mobile game Lordnine: Infinite "
    "Class. It may be a party list, a guild member list, or a rally / "
    "squad management screen.\n\n"
    "List the player character names that are shown as ACTIVE, and only "
    "those. Copy each name exactly as written, preserving capitalisation, "
    "spacing, punctuation and any non-Latin characters.\n\n"
    "Exclude a name if it is rendered dimmed, greyed out, faded, or at "
    "lower contrast than the other names around it. In this game's "
    "interface a dimmed entry means that player is not confirmed present, "
    "so it must not be listed. Compare the names against each other: the "
    "active ones share the same bright text colour, and a dimmed one is "
    "visibly darker or washed out.\n\n"
    "Also ignore: character levels, class names and icons, guild ranks "
    "and tags, HP and MP bars, damage numbers, timers, currency amounts, "
    "buttons, tab labels, chat text, the boss or monster name, and every "
    "other interface label.\n\n"
    "If the same player appears more than once (for example in both a "
    "side panel and a main grid), list them only once."
)

# Free tier: 15 requests/minute, 1,000/day -- far above expected volume.
# If accuracy proves insufficient, set GEMINI_MODEL=gemini-3.5-flash
# (10/min, 250/day), also free.
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


class VisionError(RuntimeError):
    """The screenshot could not be turned into a list of names."""


def _new_client():
    from google import genai

    return genai.Client()


def extract_names(image_bytes: bytes, mime_type: str, *, client=None) -> list[str]:
    """Return the player names visible in a roster screenshot.

    `client` is injectable so tests never touch the network. Raises
    VisionError for anything that is not a usable list of names.
    """
    client = client or _new_client()

    try:
        interaction = client.interactions.create(
            model=MODEL,
            input=[
                {"type": "text", "text": PROMPT},
                {
                    "type": "image",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                    "mime_type": mime_type,
                },
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": RESPONSE_SCHEMA,
            },
        )
    except Exception as exc:  # SDK raises assorted transport/quota errors
        raise VisionError(f"Gemini request failed: {exc}") from exc

    raw = interaction.output_text
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise VisionError(f"Gemini reply was not valid JSON: {raw!r}") from exc

    names = payload.get("names") if isinstance(payload, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise VisionError(f"Gemini reply had an unexpected shape: {payload!r}")

    cleaned = [n.strip() for n in names if n.strip()]
    if not cleaned:
        raise VisionError("Gemini found no names in that image")
    return cleaned
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_attendance_vision.py -v
```

Expected: 9 passed.

- [ ] **Step 7: Smoke test against the real API**

Unit tests prove the plumbing; only a live call proves the model reads game fonts. Save a real roster screenshot to `/tmp/roster.png` and get a key from https://aistudio.google.com/apikey:

```bash
export GEMINI_API_KEY="..."
.venv/bin/python -c "
from attendance_vision import extract_names, MODEL
print('model:', MODEL)
names = extract_names(open('/tmp/roster.png','rb').read(), 'image/png')
print(len(names), 'names found:')
for n in names: print(' ', n)
"
```

Expected: the names from the screenshot, no levels or UI labels.

If the model ID is rejected, check the live list at https://aistudio.google.com/ and re-run with `GEMINI_MODEL=<id>` — the code reads that env var, so no edit is needed. Record whichever ID worked; Task 8 puts it in the README.

- [ ] **Step 8: Confirm bot.py is untouched, then commit**

```bash
git diff --exit-code bot.py && echo "bot.py clean"
git add attendance_vision.py tests/conftest.py tests/test_attendance_vision.py requirements.txt
git commit -m "$(cat <<'EOF'
Read roster screenshots with the Gemini free tier

Uses the Interactions API with a JSON schema pinned to {"names": [...]},
so the model cannot answer with prose that would need parsing. Anything
that is not a usable list of names -- bad JSON, wrong shape, an empty
result, a quota error -- raises VisionError so the caller reports it
rather than writing an empty attendance log.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Sheet reads and batched point writes

**Files:**
- Create: `attendance_sheet.py`
- Create: `tests/test_attendance_sheet.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `attendance_bosses.header_base`
- Produces:
  - `open_spreadsheet(sheet_id: str, service_account_json: str)` → gspread `Spreadsheet`
  - `read_headers(worksheet) -> list[str]`
  - `read_players(worksheet) -> list[str]`
  - `find_column(worksheet, boss_name: str) -> int` (1-based)
  - `plan_point_writes(worksheet, players: list[str], column_index: int, points: int) -> list[dict]`
  - `apply_writes(worksheet, payload: list[dict]) -> None`
  - `SheetStructureError(RuntimeError)`
  - `HEADER_ROW = 1`, `PLAYER_COLUMN = 1`, `POINTS_COLUMN = 2`

- [ ] **Step 1: Add the fake worksheet to conftest**

Append to `tests/conftest.py`:

```python
class FakeWorksheet:
    """Stands in for a gspread Worksheet.

    Holds the grid exactly as get_all_values() returns it: every cell a
    string, blanks as "".
    """

    def __init__(self, rows: list[list[str]], title: str = "Week 17"):
        self._rows = [list(r) for r in rows]
        self.title = title
        self.batches: list[list[dict]] = []
        self.appended: list[list] = []

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def batch_update(self, data):
        self.batches.append(data)

    def append_row(self, values, **kwargs):
        self.appended.append(list(values))

    def update_cell(self, row, col, value):
        while len(self._rows) < row:
            self._rows.append([])
        target = self._rows[row - 1]
        while len(target) < col:
            target.append("")
        target[col - 1] = str(value)


SAMPLE_GRID = [
    ["Player Name", "Points", "Lucus - 3", "EGO", "Livera", "Lady Dalia"],
    ["ARCILynN", "51", "", "1", "3", "3"],
    ["xSigarilyas", "49", "3", "2", "3", "4"],
    ["Kobe", "44", "", "1", "3", "2"],
    ["wileKAMOTE卐", "36", "", "", "3", "3"],
]
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_attendance_sheet.py`:

```python
import pytest

from attendance_sheet import (
    SheetStructureError,
    apply_writes,
    find_column,
    plan_point_writes,
    read_headers,
    read_players,
)
from conftest import SAMPLE_GRID, FakeWorksheet


@pytest.fixture
def ws():
    return FakeWorksheet(SAMPLE_GRID)


def test_reads_the_header_row(ws):
    assert read_headers(ws)[:3] == ["Player Name", "Points", "Lucus - 3"]


def test_reads_players_from_column_a_skipping_the_header(ws):
    assert read_players(ws) == ["ARCILynN", "xSigarilyas", "Kobe", "wileKAMOTE卐"]


def test_finds_a_column_by_its_boss_name(ws):
    assert find_column(ws, "EGO") == 4
    assert find_column(ws, "Lady Dalia") == 6


def test_finds_a_column_whose_header_carries_a_point_annotation(ws):
    assert find_column(ws, "Lucus") == 3


def test_missing_column_names_what_it_wanted(ws):
    with pytest.raises(SheetStructureError, match="Venatus"):
        find_column(ws, "Venatus")


def test_adds_points_to_the_existing_value(ws):
    payload = plan_point_writes(ws, ["ARCILynN", "Kobe"], 4, 1)
    assert payload == [
        {"range": "D2", "values": [[2]]},
        {"range": "D4", "values": [[2]]},
    ]


def test_treats_a_blank_cell_as_zero(ws):
    assert plan_point_writes(ws, ["ARCILynN"], 3, 3) == [
        {"range": "C2", "values": [[3]]}
    ]


def test_negative_points_subtract_for_undo(ws):
    assert plan_point_writes(ws, ["Kobe"], 6, -2) == [
        {"range": "F4", "values": [[0]]}
    ]


def test_refuses_to_write_the_points_column(ws):
    with pytest.raises(SheetStructureError, match="formula"):
        plan_point_writes(ws, ["ARCILynN"], 2, 1)


def test_a_missing_player_aborts_the_whole_write(ws):
    with pytest.raises(SheetStructureError, match="Ghost"):
        plan_point_writes(ws, ["Kobe", "Ghost"], 4, 1)


def test_column_lookup_survives_a_reordered_sheet():
    ws = FakeWorksheet([
        ["Player Name", "Points", "EGO", "Lucus - 3", "Livera"],
        ["Kobe", "44", "1", "", "3"],
    ])
    assert find_column(ws, "Lucus") == 4
    assert find_column(ws, "EGO") == 3


def test_apply_writes_sends_one_batch(ws):
    payload = plan_point_writes(ws, ["ARCILynN", "Kobe", "xSigarilyas"], 4, 1)
    apply_writes(ws, payload)
    assert len(ws.batches) == 1
    assert len(ws.batches[0]) == 3


def test_apply_writes_does_nothing_when_there_is_nothing_to_write(ws):
    apply_writes(ws, [])
    assert ws.batches == []
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_attendance_sheet.py -v
```

Expected: `ModuleNotFoundError: No module named 'attendance_sheet'`

- [ ] **Step 4: Write the implementation**

Create `attendance_sheet.py`:

```python
"""Google Sheets access for the attendance log.

Cells are located by content, not by fixed coordinates: the row is the one
whose column A matches the player, the column is the one whose header
matches the boss. Reordering columns therefore breaks nothing.
"""

import json

import gspread
from google.oauth2.service_account import Credentials

from attendance_bosses import header_base

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADER_ROW = 1
PLAYER_COLUMN = 1
POINTS_COLUMN = 2  # a SUM formula -- never written


class SheetStructureError(RuntimeError):
    """The sheet does not look the way the bot needs it to."""


def open_spreadsheet(sheet_id: str, service_account_json: str):
    """Authorise with a service account and open the spreadsheet by ID."""
    creds = Credentials.from_service_account_info(
        json.loads(service_account_json), scopes=SCOPES
    )
    return gspread.authorize(creds).open_by_key(sheet_id)


def _grid(worksheet) -> list[list[str]]:
    return worksheet.get_all_values()


def read_headers(worksheet) -> list[str]:
    """The header row, verbatim."""
    grid = _grid(worksheet)
    if not grid:
        raise SheetStructureError(f"Worksheet {worksheet.title!r} is empty")
    return list(grid[HEADER_ROW - 1])


def read_players(worksheet) -> list[str]:
    """Player names in column A, below the header row."""
    return [
        row[PLAYER_COLUMN - 1].strip()
        for row in _grid(worksheet)[HEADER_ROW:]
        if row and row[PLAYER_COLUMN - 1].strip()
    ]


def find_column(worksheet, boss_name: str) -> int:
    """1-based index of the column for this boss."""
    wanted = boss_name.strip().casefold()
    for index, cell in enumerate(read_headers(worksheet), start=1):
        if header_base(cell).casefold() == wanted:
            return index
    raise SheetStructureError(
        f"No column for {boss_name!r} in worksheet {worksheet.title!r}"
    )


def _cell_number(row: list[str], column_index: int) -> int:
    """Current value of a cell, treating blanks and junk as 0."""
    if column_index - 1 >= len(row):
        return 0
    raw = row[column_index - 1].strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def plan_point_writes(
    worksheet, players: list[str], column_index: int, points: int
) -> list[dict]:
    """Build the batch payload that adds `points` for each player.

    `points` may be negative, which is how undo reverses a log. Raises
    rather than returning a partial payload if any player is missing -- a
    half-written attendance log is worse than none.
    """
    if column_index == POINTS_COLUMN:
        raise SheetStructureError("Refusing to write column B; it is a SUM formula")

    grid = _grid(worksheet)
    rows_by_player = {
        row[PLAYER_COLUMN - 1].strip(): number
        for number, row in enumerate(grid, start=1)
        if number > HEADER_ROW and row and row[PLAYER_COLUMN - 1].strip()
    }

    missing = [p for p in players if p not in rows_by_player]
    if missing:
        raise SheetStructureError(
            f"No row for {', '.join(sorted(missing))} in "
            f"worksheet {worksheet.title!r}"
        )

    payload = []
    for player in players:
        row_number = rows_by_player[player]
        current = _cell_number(grid[row_number - 1], column_index)
        payload.append(
            {
                "range": gspread.utils.rowcol_to_a1(row_number, column_index),
                "values": [[current + points]],
            }
        )
    return payload


def apply_writes(worksheet, payload: list[dict]) -> None:
    """Send every cell update as one request.

    The Sheets API allows 60 writes per minute per user; thirty players
    written individually would burn half that on a single command.
    """
    if not payload:
        return
    worksheet.batch_update(payload)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_attendance_sheet.py -v
```

Expected: 13 passed.

- [ ] **Step 6: Verify the 3-point names against the live header row**

`BOSSES_WORTH_3` uses the timer's spellings. The sheet may differ, and a mismatch silently pays 1 point instead of 3.

```bash
export SHEET_ID="..."
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"
.venv/bin/python -c "
import os
from attendance_sheet import open_spreadsheet, read_headers
from attendance_bosses import BOSSES_WORTH_3, header_base, NON_BOSS_HEADERS

sh = open_spreadsheet(os.environ['SHEET_ID'], os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
ws = sh.worksheet('Week 17')
headers = [header_base(h) for h in read_headers(ws) if header_base(h)]
headers = [h for h in headers if h.casefold() not in NON_BOSS_HEADERS]

print('COLUMNS:', headers)
print()
known = {h.casefold() for h in headers}
missing = [b for b in sorted(BOSSES_WORTH_3) if b.casefold() not in known]
print('3-pointers with NO column:', missing or 'none -- all good')
"
```

If any are reported, find the sheet's actual spelling in the printed `COLUMNS` list and correct that entry in `BOSSES_WORTH_3` in `attendance_bosses.py`. Re-run until it prints `none`.

- [ ] **Step 7: Confirm bot.py is untouched, then commit**

```bash
git diff --exit-code bot.py && echo "bot.py clean"
.venv/bin/python -m pytest tests/ -v
git add attendance_sheet.py attendance_bosses.py tests/
git commit -m "$(cat <<'EOF'
Add points to the sheet in a single batched write

Rows and columns are located by their text, so reordering columns does
not break anything. Column B is refused outright because it is a SUM
formula. If any player in a submission has no row the whole write
aborts, rather than leaving half an attendance log behind. Negative
point values are supported so undo can reuse the same path.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Config tab, audit log, and undo

**Files:**
- Modify: `attendance_sheet.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_attendance_sheet.py`

**Interfaces:**
- Consumes: `attendance_sheet.SheetStructureError`
- Produces:
  - `get_or_create_tab(spreadsheet, title: str, header: list[str])`
  - `read_config(spreadsheet) -> dict[str, str]`
  - `write_config(spreadsheet, key: str, value: str) -> None`
  - `append_log_entry(spreadsheet, entry: dict) -> None`
  - `attachment_already_logged(spreadsheet, attachment_id: str) -> bool`
  - `last_unreversed_entry(spreadsheet) -> tuple[int, dict] | None`
  - `mark_entry_reversed(spreadsheet, row_number: int) -> None`
  - `CONFIG_TAB = "_BotConfig"`, `LOG_TAB = "_BotLog"`, `LOG_HEADER: list[str]`

- [ ] **Step 1: Add a fake spreadsheet to conftest**

Append to `tests/conftest.py`:

```python
import gspread


class FakeSpreadsheet:
    """Stands in for a gspread Spreadsheet holding FakeWorksheets."""

    def __init__(self, sheets: dict[str, FakeWorksheet] | None = None):
        self._sheets = dict(sheets or {})
        self.created: list[str] = []

    def worksheet(self, title):
        try:
            return self._sheets[title]
        except KeyError:
            raise gspread.exceptions.WorksheetNotFound(title) from None

    def add_worksheet(self, title, rows=100, cols=20):
        ws = FakeWorksheet([], title=title)
        self._sheets[title] = ws
        self.created.append(title)
        return ws
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_attendance_sheet.py`:

```python
from attendance_sheet import (
    CONFIG_TAB,
    LOG_HEADER,
    LOG_TAB,
    append_log_entry,
    attachment_already_logged,
    get_or_create_tab,
    last_unreversed_entry,
    mark_entry_reversed,
    read_config,
    write_config,
)
from conftest import FakeSpreadsheet


def _entry(**overrides):
    base = {
        "timestamp": "2026-08-06T21:00:00",
        "tab": "Week 17",
        "boss": "Lucus",
        "points_each": 3,
        "message_id": "111",
        "attachment_id": "aaa",
        "confirmed_by": "officer#1",
        "players": "Kobe, Talong",
        "reversed": "",
    }
    base.update(overrides)
    return base


def test_creates_a_tab_with_its_header_when_missing():
    sh = FakeSpreadsheet()
    ws = get_or_create_tab(sh, LOG_TAB, LOG_HEADER)
    assert sh.created == [LOG_TAB]
    assert ws.appended == [LOG_HEADER]


def test_reuses_an_existing_tab():
    sh = FakeSpreadsheet({LOG_TAB: FakeWorksheet([LOG_HEADER], title=LOG_TAB)})
    get_or_create_tab(sh, LOG_TAB, LOG_HEADER)
    assert sh.created == []


def test_config_round_trips():
    sh = FakeSpreadsheet()
    write_config(sh, "target_tab", "Week 17")
    write_config(sh, "officer_role_id", "12345")
    assert read_config(sh) == {"target_tab": "Week 17", "officer_role_id": "12345"}


def test_writing_an_existing_config_key_replaces_it():
    sh = FakeSpreadsheet()
    write_config(sh, "target_tab", "Week 17")
    write_config(sh, "target_tab", "Week 17.1")
    assert read_config(sh) == {"target_tab": "Week 17.1"}


def test_config_is_empty_when_the_tab_does_not_exist():
    assert read_config(FakeSpreadsheet()) == {}


def test_log_entry_is_appended_in_header_order():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry())
    ws = sh.worksheet(LOG_TAB)
    assert ws.appended[0] == LOG_HEADER
    assert ws.appended[1][LOG_HEADER.index("boss")] == "Lucus"
    assert ws.appended[1][LOG_HEADER.index("players")] == "Kobe, Talong"


def test_detects_a_screenshot_that_was_already_logged():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(attachment_id="aaa"))
    assert attachment_already_logged(sh, "aaa") is True
    assert attachment_already_logged(sh, "bbb") is False


def test_reversed_entries_do_not_count_as_already_logged():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(attachment_id="aaa"))
    row_number, _ = last_unreversed_entry(sh)
    mark_entry_reversed(sh, row_number)
    assert attachment_already_logged(sh, "aaa") is False


def test_last_unreversed_entry_returns_the_most_recent():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(boss="Lucus", attachment_id="aaa"))
    append_log_entry(sh, _entry(boss="Motti", attachment_id="bbb"))
    row_number, entry = last_unreversed_entry(sh)
    assert entry["boss"] == "Motti"
    assert row_number == 3  # header + two entries


def test_last_unreversed_entry_skips_reversed_ones():
    sh = FakeSpreadsheet()
    append_log_entry(sh, _entry(boss="Lucus", attachment_id="aaa"))
    append_log_entry(sh, _entry(boss="Motti", attachment_id="bbb"))
    mark_entry_reversed(sh, 3)
    _, entry = last_unreversed_entry(sh)
    assert entry["boss"] == "Lucus"


def test_last_unreversed_entry_is_none_when_there_is_nothing_to_undo():
    assert last_unreversed_entry(FakeSpreadsheet()) is None
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_attendance_sheet.py -v
```

Expected: `ImportError: cannot import name 'CONFIG_TAB'`

- [ ] **Step 4: Write the implementation**

Append to `attendance_sheet.py`:

```python
CONFIG_TAB = "_BotConfig"
LOG_TAB = "_BotLog"

CONFIG_HEADER = ["key", "value"]
LOG_HEADER = [
    "timestamp",
    "tab",
    "boss",
    "points_each",
    "message_id",
    "attachment_id",
    "confirmed_by",
    "players",
    "reversed",
]


def get_or_create_tab(spreadsheet, title: str, header: list[str]):
    """Return the named worksheet, creating it with `header` if absent."""
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title, rows=1000, cols=len(header))
        worksheet.append_row(header)
        return worksheet


def read_config(spreadsheet) -> dict[str, str]:
    """Bot settings stored in the sheet itself.

    The sheet is used because Render wipes the disk on every restart, so
    a local file would not survive.
    """
    try:
        worksheet = spreadsheet.worksheet(CONFIG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return {}

    return {
        row[0].strip(): row[1].strip()
        for row in worksheet.get_all_values()[1:]
        if len(row) >= 2 and row[0].strip()
    }


def write_config(spreadsheet, key: str, value: str) -> None:
    """Set one config key, replacing any existing row for it."""
    worksheet = get_or_create_tab(spreadsheet, CONFIG_TAB, CONFIG_HEADER)

    for number, row in enumerate(worksheet.get_all_values(), start=1):
        if number > 1 and row and row[0].strip() == key:
            worksheet.update_cell(number, 2, value)
            return

    worksheet.append_row([key, value])


def append_log_entry(spreadsheet, entry: dict) -> None:
    """Record one confirmed submission in the audit tab."""
    worksheet = get_or_create_tab(spreadsheet, LOG_TAB, LOG_HEADER)
    worksheet.append_row([str(entry.get(field, "")) for field in LOG_HEADER])


def _log_rows(spreadsheet) -> list[tuple[int, dict]]:
    try:
        worksheet = spreadsheet.worksheet(LOG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return []

    rows = []
    for number, row in enumerate(worksheet.get_all_values(), start=1):
        if number == 1 or not row:
            continue
        padded = list(row) + [""] * (len(LOG_HEADER) - len(row))
        rows.append((number, dict(zip(LOG_HEADER, padded))))
    return rows


def attachment_already_logged(spreadsheet, attachment_id: str) -> bool:
    """True if this exact screenshot was logged and not later reversed."""
    return any(
        entry["attachment_id"] == attachment_id and not entry["reversed"].strip()
        for _, entry in _log_rows(spreadsheet)
    )


def last_unreversed_entry(spreadsheet) -> tuple[int, dict] | None:
    """Most recent log entry that has not been undone."""
    for number, entry in reversed(_log_rows(spreadsheet)):
        if not entry["reversed"].strip():
            return number, entry
    return None


def mark_entry_reversed(spreadsheet, row_number: int) -> None:
    """Flag a log entry as undone."""
    worksheet = spreadsheet.worksheet(LOG_TAB)
    worksheet.update_cell(row_number, LOG_HEADER.index("reversed") + 1, "yes")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_attendance_sheet.py -v
```

Expected: 24 passed (13 from Task 5, 11 new).

- [ ] **Step 6: Confirm bot.py is untouched, then commit**

```bash
git diff --exit-code bot.py && echo "bot.py clean"
git add attendance_sheet.py tests/
git commit -m "$(cat <<'EOF'
Record every attendance write in an audit tab

A number in the attendance sheet currently has no provenance -- nobody
can tell which screenshot or which officer put it there. _BotLog records
one row per confirmed submission, which also gives duplicate detection
for re-posted screenshots and makes undo possible. _BotConfig holds the
target week tab and officer role, because Render wipes the disk on every
restart.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: The attendance bot

**Files:**
- Create: `attendance_bot.py`

**Interfaces:**
- Consumes: everything from Tasks 2–6. **Must not import `bot.py`.**
- Produces: the second bot's entry point and five commands.

- [ ] **Step 1: Write the module**

Create `attendance_bot.py`:

```python
"""Attendance logging bot for the Lordnine guild.

A second Discord application, separate from the field boss timer. It does
not import bot.py, share its token, or run in its process -- the timer is
in production and must not be affected by anything here.

Started by supervisor.py. Exits with EXIT_NOT_CONFIGURED when its
credentials are absent, which the supervisor treats as a deliberate stop,
so the timer runs normally on a deploy that predates these secrets.
"""

import asyncio
import os
import sys
from datetime import datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv

from attendance_bosses import BossAmbiguous, BossNotFound, boss_points, resolve_boss
from attendance_roster import match_names
from attendance_sheet import (
    SheetStructureError,
    append_log_entry,
    apply_writes,
    attachment_already_logged,
    find_column,
    last_unreversed_entry,
    mark_entry_reversed,
    open_spreadsheet,
    plan_point_writes,
    read_config,
    read_headers,
    read_players,
    write_config,
)
from attendance_vision import VisionError, extract_names

load_dotenv()

EXIT_NOT_CONFIGURED = 78  # must match supervisor.NO_RESTART_CODES

TOKEN = os.getenv("ATTENDANCE_DISCORD_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

CONFIRM_EMOJI = "✅"
PREVIEW_TIMEOUT = 180  # seconds
EMBED_COLOR = discord.Color.orange()  # matches the timer's look

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def make_embed(
    title: str, description: str | None = None, footer: str | None = None
) -> discord.Embed:
    """Same shape as the timer's embeds, implemented locally.

    Deliberately duplicated rather than imported: importing bot.py would
    reintroduce the coupling this whole design removes.
    """
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    if footer:
        embed.set_footer(text=footer)
    return embed


# ---------------------------------------------------------------------------
# Blocking helpers. Every one runs through asyncio.to_thread: gspread and
# google-genai are synchronous, and blocking this bot's event loop would
# make it stop answering commands.
# ---------------------------------------------------------------------------


def _spreadsheet():
    return open_spreadsheet(SHEET_ID, SERVICE_ACCOUNT_JSON)


def _officer_role_id() -> int | None:
    raw = read_config(_spreadsheet()).get("officer_role_id", "")
    return int(raw) if raw.isdigit() else None


def _is_officer(member, role_id: int | None) -> bool:
    if role_id is None:
        return False
    return any(role.id == role_id for role in getattr(member, "roles", []))


def _load_context(boss_query: str, attachment_id: str) -> dict:
    """Everything the preview needs, in one trip to Sheets."""
    spreadsheet = _spreadsheet()
    config = read_config(spreadsheet)

    tab = config.get("target_tab")
    if not tab:
        raise SheetStructureError("No target tab set. Run !setweek <tab name> first.")

    worksheet = spreadsheet.worksheet(tab)
    boss = resolve_boss(read_headers(worksheet), boss_query)

    return {
        "tab": tab,
        "boss": boss,
        "points": boss_points(boss),
        "players": read_players(worksheet),
        "column": find_column(worksheet, boss),
        "duplicate": attachment_already_logged(spreadsheet, attachment_id),
    }


def _commit(tab: str, column: int, players: list[str], points: int,
            entry: dict) -> None:
    spreadsheet = _spreadsheet()
    worksheet = spreadsheet.worksheet(tab)
    apply_writes(worksheet, plan_point_writes(worksheet, players, column, points))
    append_log_entry(spreadsheet, entry)


def _reverse_last() -> dict | None:
    spreadsheet = _spreadsheet()
    found = last_unreversed_entry(spreadsheet)
    if found is None:
        return None

    row_number, entry = found
    players = [p.strip() for p in entry["players"].split(",") if p.strip()]
    worksheet = spreadsheet.worksheet(entry["tab"])
    column = find_column(worksheet, entry["boss"])

    apply_writes(
        worksheet,
        plan_point_writes(worksheet, players, column, -int(entry["points_each"])),
    )
    mark_entry_reversed(spreadsheet, row_number)
    return entry


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def _reject(ctx, title: str, description: str, footer: str | None = None):
    await ctx.send(embed=make_embed(title, description, footer=footer))


async def _require_officer(ctx) -> bool:
    try:
        role_id = await asyncio.to_thread(_officer_role_id)
    except Exception as exc:
        await _reject(ctx, "❌ Sheet Unreachable", str(exc))
        return False

    if role_id is None:
        await _reject(
            ctx,
            "⚙️ Not Configured Yet",
            "No officer role is set.",
            footer="An admin must run !setofficerrole @role",
        )
        return False

    if not _is_officer(ctx.author, role_id):
        await _reject(
            ctx, "\U0001f6ab Officers Only", "Only officers can record attendance."
        )
        return False
    return True


@bot.event
async def on_ready():
    print(f"Attendance bot logged in as {bot.user} (ID: {bot.user.id}).", flush=True)


@bot.command(name="attendance")
async def attendance_cmd(ctx: commands.Context, boss_name: str = ""):
    """Log attendance from a roster screenshot: !attendance <boss> + image"""
    if not await _require_officer(ctx):
        return

    if not boss_name:
        await _reject(
            ctx,
            "❓ Which Boss?",
            "Usage: `!attendance <boss>` with a roster screenshot attached.",
            footer="e.g. !attendance Lucus",
        )
        return

    if not ctx.message.attachments:
        await _reject(
            ctx,
            "\U0001f5bc️ No Screenshot",
            "Attach a party or guild roster screenshot to the same message.",
        )
        return

    attachment = ctx.message.attachments[0]
    if not (attachment.content_type or "").startswith("image/"):
        await _reject(
            ctx, "\U0001f5bc️ Not An Image", "That attachment is not an image."
        )
        return

    working = await ctx.send(
        embed=make_embed("\U0001f50e Reading Screenshot", "Working on it...")
    )

    try:
        context = await asyncio.to_thread(_load_context, boss_name, str(attachment.id))
    except (BossNotFound, BossAmbiguous) as exc:
        await working.edit(embed=make_embed("❓ Unknown Boss", str(exc)))
        return
    except Exception as exc:
        await working.edit(embed=make_embed("❌ Sheet Problem", str(exc)))
        return

    boss, points = context["boss"], context["points"]
    await working.edit(
        embed=make_embed("\U0001f50e Reading Screenshot", f"Working on **{boss}**...")
    )

    try:
        image_bytes = await attachment.read()
        raw_names = await asyncio.to_thread(
            extract_names, image_bytes, attachment.content_type
        )
    except VisionError as exc:
        await working.edit(
            embed=make_embed(
                "❌ Couldn't Read That",
                str(exc),
                footer="Try a clearer or less cropped screenshot.",
            )
        )
        return

    matched, unmatched = match_names(raw_names, context["players"])
    if not matched:
        await working.edit(
            embed=make_embed(
                "❌ No Known Players Found",
                f"None of the names matched a player in **{context['tab']}**."
                f"\n\nRead: {', '.join(raw_names)}",
            )
        )
        return

    players = [m.player for m in matched]
    embed = make_embed(
        "\U0001f4cb Confirm Attendance",
        f"**{boss}** — **+{points}** point{'s' if points != 1 else ''} each, "
        f"into **{context['tab']}**",
        footer=f"React {CONFIRM_EMOJI} within {PREVIEW_TIMEOUT}s to write. "
               "Nothing is saved until you do.",
    )
    embed.add_field(
        name=f"✅ Matched ({len(players)})",
        value="\n".join(players)[:1024],
        inline=False,
    )
    if unmatched:
        embed.add_field(
            name=f"❓ Not Recognised ({len(unmatched)})",
            value=("\n".join(unmatched)[:1024]
                   + "\n\n*Skipped. Add them to the sheet first if they count.*"),
            inline=False,
        )
    if context["duplicate"]:
        embed.add_field(
            name="⚠️ Already Logged",
            value="This exact screenshot has been logged before. "
                  "Confirming will add the points again.",
            inline=False,
        )

    await working.edit(embed=embed)
    await working.add_reaction(CONFIRM_EMOJI)

    role_id = await asyncio.to_thread(_officer_role_id)

    def check(reaction, user):
        return (
            reaction.message.id == working.id
            and str(reaction.emoji) == CONFIRM_EMOJI
            and user.id != bot.user.id
            and _is_officer(user, role_id)
        )

    try:
        _, confirmer = await bot.wait_for(
            "reaction_add", check=check, timeout=PREVIEW_TIMEOUT
        )
    except asyncio.TimeoutError:
        await working.edit(
            embed=make_embed(
                "⏱️ Expired",
                f"No confirmation within {PREVIEW_TIMEOUT}s. Nothing was written.",
            )
        )
        return

    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "tab": context["tab"],
        "boss": boss,
        "points_each": points,
        "message_id": str(ctx.message.id),
        "attachment_id": str(attachment.id),
        "confirmed_by": str(confirmer),
        "players": ", ".join(players),
        "reversed": "",
    }

    try:
        await asyncio.to_thread(
            _commit, context["tab"], context["column"], players, points, entry
        )
    except SheetStructureError as exc:
        await working.edit(
            embed=make_embed("❌ Write Failed", f"{exc}\n\nNothing was written.")
        )
        return

    await working.edit(
        embed=make_embed(
            "✅ Attendance Recorded",
            f"**+{points}** for **{boss}** — {len(players)} player"
            f"{'s' if len(players) != 1 else ''} in **{context['tab']}**.",
            footer=f"Confirmed by {confirmer} • !undoattendance reverses this",
        )
    )


@bot.command(name="undoattendance")
async def undo_attendance_cmd(ctx: commands.Context):
    """Reverse the most recent attendance log: !undoattendance"""
    if not await _require_officer(ctx):
        return

    try:
        entry = await asyncio.to_thread(_reverse_last)
    except (SheetStructureError, BossNotFound, BossAmbiguous) as exc:
        await _reject(ctx, "❌ Undo Failed", str(exc))
        return

    if entry is None:
        await _reject(ctx, "ℹ️ Nothing To Undo", "No attendance log found.")
        return

    await ctx.send(
        embed=make_embed(
            "↩️ Attendance Reversed",
            f"Removed **{entry['points_each']}** points for "
            f"**{entry['boss']}** from **{entry['tab']}**.",
            footer=f"Originally logged {entry['timestamp']} "
                   f"by {entry['confirmed_by']}",
        )
    )


@bot.command(name="setweek")
async def set_week_cmd(ctx: commands.Context, *, tab_name: str = ""):
    """Choose which sheet tab attendance goes into: !setweek Week 17.1"""
    if not await _require_officer(ctx):
        return

    tab = tab_name.strip()
    if not tab:
        await _reject(
            ctx, "❓ Which Tab?", "Usage: `!setweek <tab name>`",
            footer="e.g. !setweek Week 17.1",
        )
        return

    def apply():
        spreadsheet = _spreadsheet()
        spreadsheet.worksheet(tab)  # raises if it does not exist
        write_config(spreadsheet, "target_tab", tab)

    try:
        await asyncio.to_thread(apply)
    except Exception as exc:
        await _reject(
            ctx, "❌ No Such Tab", f"Couldn't open a tab named `{tab}`.\n\n{exc}"
        )
        return

    await ctx.send(
        embed=make_embed(
            "✅ Target Tab Set", f"Attendance will be written to **{tab}**."
        )
    )


@bot.command(name="setofficerrole")
@commands.has_permissions(administrator=True)
async def set_officer_role_cmd(ctx: commands.Context, role: discord.Role):
    """Choose which role may log attendance: !setofficerrole @Officer"""
    await asyncio.to_thread(
        lambda: write_config(_spreadsheet(), "officer_role_id", str(role.id))
    )
    await ctx.send(
        embed=make_embed(
            "✅ Officer Role Set", f"{role.mention} can now record attendance."
        )
    )


@bot.command(name="attendancehelp")
async def attendance_help_cmd(ctx: commands.Context):
    """Show the attendance commands: !attendancehelp"""
    embed = make_embed(
        "\U0001f4cb Attendance Commands",
        "Log guild attendance from an in-game roster screenshot.",
        footer="Points are added to the Point System sheet",
    )
    embed.add_field(
        name="!attendance <boss>",
        value="Attach a roster screenshot. Officers only.", inline=False,
    )
    embed.add_field(
        name="!undoattendance",
        value="Reverse the last log. Officers only.", inline=False,
    )
    embed.add_field(
        name="!setweek <tab>",
        value="Set the target tab, e.g. `Week 17.1`.", inline=False,
    )
    embed.add_field(
        name="!setofficerrole @role",
        value="Admins only. Sets who may log.", inline=False,
    )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    missing = [
        name
        for name, value in (
            ("ATTENDANCE_DISCORD_TOKEN", TOKEN),
            ("SHEET_ID", SHEET_ID),
            ("GOOGLE_SERVICE_ACCOUNT_JSON", SERVICE_ACCOUNT_JSON),
            ("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY")),
        )
        if not value
    ]
    if missing:
        # Exit deliberately so the supervisor leaves this stopped instead
        # of crash-looping. The timer is unaffected.
        print(
            "Attendance bot not started; missing: " + ", ".join(missing),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_NOT_CONFIGURED)

    bot.run(TOKEN)
```

- [ ] **Step 2: Verify it imports cleanly and never touches bot.py**

```bash
.venv/bin/python -c "
import sys
import attendance_bot
assert 'bot' not in sys.modules, 'attendance_bot must not import bot.py'
names = sorted(c.name for c in attendance_bot.bot.commands)
print('commands:', names)
for expected in ['attendance','undoattendance','setweek','setofficerrole','attendancehelp']:
    assert expected in names, f'missing {expected}'
print('OK: five commands, bot.py never imported')
"
```

Expected: the five command names, then `OK: five commands, bot.py never imported`.

- [ ] **Step 3: Verify the unconfigured exit code**

This is the property that makes deploying before the secrets exist safe.

```bash
env -u ATTENDANCE_DISCORD_TOKEN -u SHEET_ID \
    -u GOOGLE_SERVICE_ACCOUNT_JSON -u GEMINI_API_KEY \
    .venv/bin/python attendance_bot.py; echo "exit code: $?"
```

Expected: a `missing:` line listing all four, then `exit code: 78`.

Note: if a `.env` file supplies any of these locally, temporarily rename it for this check — `load_dotenv()` reads it.

- [ ] **Step 4: Run the whole test suite**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected: 67 passed (9 supervisor + 14 bosses + 11 roster + 9 vision + 24 sheet).

- [ ] **Step 5: Confirm bot.py is untouched, then commit**

```bash
git diff --exit-code bot.py && echo "bot.py clean"
git add attendance_bot.py
git commit -m "$(cat <<'EOF'
Add the attendance bot

A second Discord application with its own token and its own commands.
It never imports bot.py, so nothing here can break the timer.

Nothing reaches the sheet without an officer reacting to a preview that
shows which names matched, which did not, and how many points are about
to be added. Sheets and Gemini calls run in worker threads so a slow
request never blocks the bot's event loop.

Missing credentials exit 78, which the supervisor reads as a deliberate
stop -- so this can ship before the secrets are configured.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Deployment wiring and documentation

**Files:**
- Modify: `render.yaml`
- Modify: `README.md`

- [ ] **Step 1: Point Render at the supervisor**

In `render.yaml`, change the start command:

```yaml
    startCommand: python -u supervisor.py
```

Then add the four new secrets to `envVars`, keeping the existing three exactly as they are:

```yaml
      - key: ATTENDANCE_DISCORD_TOKEN
        sync: false
      - key: SHEET_ID
        sync: false
      - key: GOOGLE_SERVICE_ACCOUNT_JSON
        sync: false
      - key: GEMINI_API_KEY
        sync: false
```

- [ ] **Step 2: Verify the config parses and bot.py is untouched**

```bash
.venv/bin/pip install pyyaml   # check-only; do NOT add to requirements.txt
.venv/bin/python -c "
import yaml
svc = yaml.safe_load(open('render.yaml'))['services'][0]
keys = [e['key'] for e in svc['envVars']]
print('start:', svc['startCommand'])
print('env keys:', keys)
assert svc['startCommand'] == 'python -u supervisor.py'
for key in ['DISCORD_TOKEN','BOT_TZ','ATTENDANCE_DISCORD_TOKEN','SHEET_ID',
            'GOOGLE_SERVICE_ACCOUNT_JSON','GEMINI_API_KEY']:
    assert key in keys, key
print('OK')
"
git diff --exit-code bot.py && echo "bot.py clean"
```

- [ ] **Step 3: Document the feature**

Add this immediately after the existing command table in `README.md`:

```markdown

**Attendance bot** (a separate bot — see below):

| Command | What it does |
|---|---|
| `!attendance <boss>` | Officers only. Attach an in-game roster screenshot; the bot reads the names and adds that boss's points to the Point System sheet. Shows a preview first — nothing is written until an officer reacts ✅. |
| `!undoattendance` | Officers only. Reverses the most recent attendance log. |
| `!setweek <tab>` | Officers only. Sets which sheet tab attendance goes into, e.g. `Week 17.1`. |
| `!setofficerrole @role` | Admins only. Sets which role may record attendance. |
| `!attendancehelp` | Lists the attendance commands. |
```

Then add this section immediately before `## Deploy 24/7 on Render (free tier)`:

```markdown
## Attendance logging

Attendance points live in the *Point System* Google Sheet: one row per
player, one column per boss, and a cell value that accumulates points.
Most bosses are worth 1 point per attendance; `Lucus`, `Libitina`,
`Rakajeth`, `Icaruthia`, `Motti`, `Nevaeh`, `Tumier` and `Camalia` are
worth 3.

### Two bots, one service

Attendance is a **separate Discord bot** with its own token and its own
code. It never imports `bot.py`. `supervisor.py` starts both as
independent processes, so a crash or a bad deploy on the attendance side
cannot stop the timer.

They share one Render service deliberately. Render's 750 free instance
hours are shared across a workspace, so two services running 24/7 would
exhaust them around the 16th of each month — and Render then suspends
*every* free service, timer included.

### Setup

1. **Second Discord application** — at the
   [Developer Portal](https://discord.com/developers/applications), create a
   new application and bot, enable **MESSAGE CONTENT INTENT**, and invite it
   to your server. Its token becomes `ATTENDANCE_DISCORD_TOKEN`.
2. **Gemini API key** — create one at
   [aistudio.google.com/apikey](https://aistudio.google.com/apikey). No credit
   card, no billing account; the free tier allows 1,000 requests a day. Note
   that on the free tier Google may use submitted content to improve its
   products.
3. **Service account** — in [Google Cloud Console](https://console.cloud.google.com),
   create a project, enable the **Google Sheets API**, create a service
   account, and download its JSON key.
4. **Share the sheet** — open the Point System sheet, press Share, and give
   the service account's email **Editor** access. Without this the bot cannot
   see the sheet at all.
5. **Set the env vars** on Render: `ATTENDANCE_DISCORD_TOKEN`,
   `GEMINI_API_KEY`, `SHEET_ID` (the long ID in the sheet's URL) and
   `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole JSON key, pasted as one line).
6. **In Discord**, run `!setofficerrole @Officer` then `!setweek "Week 17"`.

Until those variables are set, the attendance bot exits cleanly and the
supervisor leaves it stopped — **the timer runs normally either way**, so
this is safe to deploy before the credentials exist.

The bot creates two hidden tabs in the sheet on first use: `_BotConfig`
(target tab and officer role, which survive Render restarts) and `_BotLog`
(one row per confirmed submission, which is what makes `!undoattendance`
and duplicate detection work). Don't delete them.
```

- [ ] **Step 4: Test end-to-end against a COPY of the sheet**

Do not point this at the live Point System sheet yet. In Google Sheets use **File → Make a copy**, share the copy with the service account, and set `SHEET_ID` to the copy.

⚠️ Stop the Render service first, or run this while it is asleep — two instances on the timer's token produce duplicate boss notifications, as `README.md` already warns.

```bash
export DISCORD_TOKEN="..." ATTENDANCE_DISCORD_TOKEN="..."
export GEMINI_API_KEY="..." SHEET_ID="<the COPY>"
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"
.venv/bin/python supervisor.py
```

Verify in Discord:

1. Both bots come online; `[supervisor] starting timer` and `starting attendance` both appear.
2. `!bosses` still works — the timer is unaffected.
3. `!attendancehelp` lists the five commands.
4. `!setofficerrole @Officer`, then `!setweek "Week 17"`.
5. A non-officer running `!attendance Lucus` is refused.
6. `!attendance lucus` with a real screenshot shows a preview; check the matched names against the screenshot by eye.
7. React ✅ → the copy's `Lucus` column gains 3 for each matched player, and column B recalculates itself.
8. Re-post the same screenshot → the preview carries the "Already Logged" warning.
9. `!undoattendance` → the points come back off and `_BotLog` shows `reversed`.
10. **Isolation check:** find the attendance child's PID and `kill -9` it. The supervisor must restart it, `!bosses` must keep working throughout, and no boss notification may be missed.

- [ ] **Step 5: Commit and open the pull request**

```bash
git diff --exit-code bot.py && echo "bot.py clean -- final check"
git add render.yaml README.md
git commit -m "$(cat <<'EOF'
Run both bots under the supervisor on Render and document the setup

The start command moves to supervisor.py, which launches bot.py and
attendance_bot.py as separate processes. Adds the four attendance
secrets and a README section covering the second Discord application,
the Gemini key, the service account, and why both bots share one service.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"

git push -u origin attendance-logging
gh pr create --title "Log guild attendance from roster screenshots" --body "$(cat <<'EOF'
## What

`!attendance <boss>` with an in-game roster screenshot attached reads the
player names and adds that boss's point value to each matched player's row
in the Point System sheet.

## The timer is not touched

Attendance is a **separate Discord bot** with its own token and its own
code. It never imports `bot.py`. `supervisor.py` runs both as independent
processes, so a crash, a bad deploy, an unhandled exception or a blocked
event loop on the attendance side cannot stop the timer.

`git diff main -- bot.py` is empty.

Both share one Render service deliberately: the 750 free instance hours are
shared per workspace, so two 24/7 services would exhaust them mid-month and
Render would then suspend *every* free service, timer included.

## How it works

- **Vision:** Gemini free tier (`gemini-3.1-flash-lite`), JSON-schema
  constrained. No credit card, 1,000 requests/day.
- **Boss names** resolve against the sheet's own header row, so there is no
  mapping table to drift and a new column is loggable with no code change.
- **Matching:** names match against the players already in column A — the bot
  never invents a row. Ambiguous or unrecognised names are reported, never
  guessed.
- **Safety:** a preview shows matched names, unmatched names and the points
  about to be added. Nothing is written until an officer reacts.
- **Audit:** a hidden `_BotLog` tab records every write, giving duplicate
  detection and `!undoattendance`.
- **Column B is never written** — it is a SUM formula.
- Missing attendance credentials exit 78 and the supervisor leaves that child
  stopped, so this is safe to deploy before the secrets exist.

## Testing

67 unit tests across the supervisor, point values, name matching, vision
parsing and sheet writes. Manually verified end-to-end against a *copy* of
the Point System sheet, including killing the attendance process to confirm
the timer keeps running.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage** — every section of `2026-08-06-attendance-logging-design.md` (revision 2) maps to a task:

| Spec section | Task |
|---|---|
| Hard constraint: timer not at risk | 1, verified in every commit step |
| Architecture / two processes | 1 |
| Isolation properties table | 1 (tests), 8 (manual kill test) |
| Boss names from the sheet header row | 2 |
| Point values | 2, verified against the live sheet in 5 |
| Name matching | 3 |
| Vision extraction | 4 |
| Sheet writes, Column B protection | 5 |
| Audit log, duplicate detection, undo | 6 |
| Commands, permission gate, preview/confirm | 7 |
| Config in `_BotConfig` | 6 (storage), 7 (commands) |
| Exit 78 when unconfigured | 1 (supervisor side), 7 (bot side) |
| Error handling table | 2, 4, 5, 7 |
| Testing | 1–6 unit, 8 end-to-end on a copy |
| Env vars, README | 8 |

**Known gaps, accepted:**

- Adding a new player row from the preview is out of scope. Unmatched names are reported and the officer adds the row in Sheets. Writing a new row from a fuzzy screenshot read is the highest-risk operation in the feature.
- Only exact duplicate-screenshot detection is implemented, not a "same boss logged recently" time window — that would fire falsely whenever a boss genuinely is run twice.
- `attendance_bot.py` has no unit tests; it is thin glue over five tested modules, and Task 7 Steps 2–3 assert its two structural properties (no `bot.py` import, exit 78). Its behaviour is covered by the Task 8 end-to-end script.

**Type consistency across tasks:** `Match(raw, player, score)` is produced in Task 3 and read as `m.player` in Task 7. `plan_point_writes` / `apply_writes` are defined in Task 5 and reused with negative points for undo in Tasks 6–7. `LOG_HEADER` field names from Task 6 are the exact dict keys built in Task 7. `EXIT_NOT_CONFIGURED = 78` is defined in Task 1 and re-declared in Task 7 with a comment tying the two together. `header_base` is defined in Task 2 and imported by `attendance_sheet` in Task 5.
