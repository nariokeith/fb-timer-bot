# Screenshot Attendance Logging — Design

Date: 2026-08-06
Status: approved design, not yet implemented
Revision: 2 — two separate bots, supervised as independent processes

## Context

The Lordnine field boss timer bot (`bot.py`) runs 24/7 on Render's free tier and
is in production. Guild attendance is tracked separately, by hand, in a Google
Sheet called *Point System*: one row per player, one column per boss, and a cell
value that accumulates points. An officer types the numbers in after each run.

This feature lets an officer post a screenshot of the in-game party/guild roster
to Discord and have the bot add the points automatically.

## Hard constraint

**The production timer must not be put at risk.** `bot.py` is not modified, not
imported, and does not share a process with the new code.

Attendance runs as a **second Discord bot** — its own application, its own
token, its own code — started as a **separate OS process** by a small
supervisor. A crash, a bad deploy, an unhandled exception, or an import error in
the attendance bot cannot stop the timer.

### Why not two Render services

Render grants **750 free instance hours per month per workspace, shared across
all free services**. Two services running 24/7 consume roughly 1,460 hours,
exhausting the budget around the 16th of each month — at which point Render
**suspends every free service in the workspace, including the timer**. Two free
services would therefore damage the thing this design exists to protect.

One service running two processes stays at ~730 hours, comfortably inside the
budget. Paying for a second service (~$7/month) remains an option later if
shared RAM or shared deploys become a problem; nothing in this design prevents
splitting them apart.

## Goals

- An officer posts `!attendance <boss>` with a roster screenshot; matched players
  get that boss's point value added to the current week's tab.
- Nothing reaches the sheet without a human confirming what was read.
- Every write is auditable and reversible.
- The timer keeps running no matter what the attendance bot does.

## Non-goals

- Changing how points are earned or what a boss is worth.
- Creating or rolling over weekly tabs. The officer does that by hand and points
  the bot at the right tab.
- Reading anything other than an in-game party/guild roster panel.
- Any change to the timer's behaviour, commands, notifications, or state.

## Architecture

```
Render service (one, free tier, 512 MB)
│
└── supervisor.py                         ← the start command
    │
    ├── subprocess: python -u bot.py      ← UNCHANGED. Own token (DISCORD_TOKEN).
    │                                        Binds $PORT for the uptime pinger.
    │
    └── subprocess: python -u attendance_bot.py
                                          ← Own token (ATTENDANCE_DISCORD_TOKEN).
                                             Binds nothing. Never imports bot.py.
```

The supervisor starts both children, waits, and restarts either one if it exits
unexpectedly, with a backoff. It forwards `SIGTERM` so Render's shutdown is
clean. A child that exits deliberately — code `0`, or code `78` meaning "not
configured" — is **not** restarted, so a missing attendance API key produces one
clear log line instead of a crash loop.

Only `bot.py` binds `$PORT`; the attendance bot has no web server, so there is no
port conflict and the existing UptimeRobot monitor is unaffected.

### Isolation properties this buys

| Risk | Mitigated by |
|---|---|
| attendance import error stops the timer | separate process; timer starts independently |
| attendance exception kills the shared bot | separate process and separate `Bot` instance |
| attendance blocks the event loop, delaying spawn alerts | separate process, separate event loop |
| command name collision between the two bots | separate Discord applications |
| attendance code changes break timer code | `bot.py` never imported |

Shared deploys and shared RAM remain. A redeploy restarts both, which the timer
already tolerates — it restores state from its pinned Discord message.

### Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `supervisor.py` | start, supervise, restart, signal handling | stdlib only |
| `attendance_bot.py` | Discord surface, permission gate, preview/confirm | discord.py + the three below |
| `attendance_vision.py` | screenshot bytes → list of name strings | google-genai |
| `attendance_roster.py` | raw names → players in the sheet, or unmatched | stdlib only |
| `attendance_sheet.py` | sheet reads, batched writes, config tab, audit log | gspread |
| `attendance_bosses.py` | point values, boss-name → column resolution | stdlib only |

`attendance_roster` and `attendance_bosses` are pure and test without network.
`vision` and `sheet` take injectable clients so their tests do not hit the wire.

## Data flow

```
!attendance <boss>  +  attached image
        │
        ├─ gate: author holds the officer role         → reject
        ├─ read the target tab's header row
        ├─ resolve <boss> against those headers        → unknown → reject, list options
        ├─ check _BotLog for this attachment ID
        │
        ├─ vision: Gemini reads the roster
        │     model: gemini-3.1-flash-lite (free tier)
        │     response_format pins {"names": ["..."]}
        │
        ├─ roster: fuzzy-match against column A
        │
        ├─ PREVIEW EMBED — matched · unmatched · points · warnings
        │     nothing written; waits for ✓ from an officer
        │
        └─ on confirm → batched write + _BotLog row + result embed
```

## Boss names come from the sheet

There is no mapping table between timer boss names and sheet columns, because
the attendance bot does not know about the timer's boss list. The **sheet's own
header row is the source of truth** for which bosses have attendance columns.

`!attendance dalia` resolves by matching `dalia` against the header row, using
the same convention `bot.py` uses for its own commands: case-insensitive exact
match, then unique prefix match. Headers may carry a point annotation
(`Lucus - 3`), so comparison uses the text before any ` - ` suffix.

An input matching no header, or matching more than one, is rejected with a
message naming the candidates. It never writes to a guessed column.

This removes an entire class of drift: adding a boss column to the sheet makes it
loggable immediately, with no code change.

### Point values

Hard-coded, keyed by the sheet's header base name. Values do not change.

```python
BOSSES_WORTH_3 = frozenset({
    "Lucus", "Libitina", "Rakajeth", "Icaruthia",
    "Motti", "Nevaeh", "Tumier", "Camalia",
})
```

Everything else is worth 1. The spellings above are the timer's; they are
verified against the live header row during implementation and corrected there
if the sheet spells any of them differently. A name in this set that matches no
column is reported at startup rather than silently paying 1 point forever.

## Name matching

The bot never invents player rows. It matches against the closed set of names in
column A of the target tab — roughly 35 strings — which is what makes a free OCR
engine sufficient. This is constrained matching, not open transcription.

Staged, most confident first:

1. exact match
2. case-insensitive, whitespace-normalized match
3. normalized edit distance above a fixed threshold, single clear winner only

A name with no match, or two candidates too close to separate, is reported as
**unmatched** rather than guessed. Unmatched names appear in the preview, where
the officer corrects the spelling and re-runs, or adds the player to the sheet.

Player names contain non-ASCII characters and parenthetical aliases
(`wileKAMOTE卐`, `BudoySul (Riuz)`, `chinchong ni Mumu`), so normalization is
Unicode-aware and does not strip non-ASCII characters.

## Sheet writes

Cells are located **by content, not coordinates**: the row is the one whose
column A matches the player, the column is the one whose header matches the
boss. Reordering columns does not break anything.

```
new_value = (existing cell value or 0) + boss_points(boss)
```

- **Column B (`Points`) is never written.** It is a SUM formula.
- All updates for one submission go out as a single `batch_update` — thirty
  individual writes would spend half the Sheets API's 60-per-minute quota.
- If any player cannot be located, the whole write aborts. No partial logs.

## Audit log and undo

A hidden `_BotLog` tab records one row per confirmed submission:

`timestamp · tab · boss · points each · message id · attachment id · confirmed by · players · reversed`

Today a number in the sheet has no provenance. This provides:

- **Duplicate detection** — a matching attachment ID means that exact screenshot
  was already logged. Surfaced as a warning in the preview, not a hard block.
- **`!undoattendance`** — subtracts what the last un-reversed entry added and
  marks it reversed.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!attendance <boss>` + image | officer role | preview → ✓ → write |
| `!undoattendance` | officer role | reverse the last confirmed log |
| `!setweek <tab name>` | officer role | set the target tab, e.g. `Week 17.1` |
| `!setofficerrole @role` | administrator | set who may log |
| `!attendancehelp` | anyone | usage |

Replies use an orange embed matching the timer's look, implemented locally — the
attendance bot does not import the timer's helpers.

Because this is a separate Discord application, its `!` commands are answered
only by it. There is no collision with the timer's `!killed`, `!bosses`, etc.

## Configuration and secrets

Runtime config lives in a `_BotConfig` tab in the Sheet — already authenticated,
already persistent, and it survives Render wiping the disk. It holds the target
tab name and the officer role ID.

Render environment variables:

| Var | Used by | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | timer | unchanged |
| `BOT_TZ` | timer | unchanged |
| `ATTENDANCE_DISCORD_TOKEN` | attendance | second bot application's token |
| `GEMINI_API_KEY` | attendance | Google AI Studio key; no billing account |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | attendance | service account credentials |
| `SHEET_ID` | attendance | the *Point System* spreadsheet ID |

If the attendance variables are absent, `attendance_bot.py` exits with code 78
and the supervisor leaves it stopped. **The timer runs normally.** This makes the
feature safely deployable before its credentials exist.

Setup also requires sharing the spreadsheet with the service account's email as
an Editor.

## Error handling

Every failure produces an embed naming the cause and the fix, and leaves the
sheet untouched.

| Failure | Response |
|---|---|
| no image attached | usage embed |
| boss matches no column | names the input and lists close headers |
| boss matches several columns | names the candidates |
| Gemini rate limited or errors | says so, invites retry; nothing written |
| model returns zero names | reports it rather than logging nothing |
| no player matched | reports the raw names it read |
| Sheets API error mid-write | batch is atomic; reports failure |
| confirmation not given within timeout | preview expires, nothing written |
| non-officer invokes, or reacts | refused |

## Testing

- `roster`: exact, case, whitespace, non-ASCII, near-miss OCR, ambiguous pairs,
  duplicate reads. Pure, no network.
- `bosses`: point values; header resolution including prefix, annotation
  suffixes, ambiguity, and misses.
- `vision`: mocked client — good reply, empty names, malformed JSON, wrong
  shape, API error.
- `sheet`: locating cells after reordering; refusing column B; aborting on a
  missing player; config round-trip; duplicate detection; undo.
- `supervisor`: a dying child is restarted; a child exiting 0 or 78 is not; the
  timer child is unaffected by the attendance child's death.
- Manual end-to-end on a **copy** of the Point System sheet before the live one.

## Inputs still needed at implementation time

1. A second Discord application and bot token, with the Message Content intent
   enabled and the bot invited to the server.
2. The spreadsheet ID of *Point System*.
3. The Discord role that counts as officer.
4. A Gemini API key from Google AI Studio.
5. A Google Cloud service account with the Sheets API enabled, and the sheet
   shared with its email address.
