## Fix Report - review follow-up

Fix 1: Added `_same_player()` in `items_bot.py` and switched the `!iam` holder check plus the `!bind` displacement check to compare through `items_rules.resolve_ign()` against the current roster. Added regressions for alias-held claims on both commands.

Fix 2: Moved the `!iam` not-a-player check inside `_SHEET_LOCK` as the first locked operation, preserving the existing sheet read, resolution, holder check, and mutation inside the lock. Added the queued-lock regression test; it was deterministic with only `asyncio.sleep(0)`. Reset `_SHEET_LOCK` in the autouse test fixture so the deliberate lock contention does not leak an event-loop-bound lock into later tests.

Fix 3: Extended the command registration and raffle channel classification tests to cover `iam`, `bind`, and `notaplayer`.

Verification command:

```text
./.venv/bin/python -m pytest -q
```

Output:

```text
2 failed, 630 passed, 42 warnings in 37.09s
FAILED tests/test_supervisor.py::test_run_handles_sigterm_and_leaves_no_orphans
FAILED tests/test_supervisor.py::test_a_second_signal_during_shutdown_does_not_crash
```

The two failures are the pre-authorized `test_supervisor.py` subprocess sandbox artifact category. No other failures remained.
