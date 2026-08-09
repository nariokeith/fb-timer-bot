"""Check the item bot's credentials and sheet before running it for real.

Run this first. It answers, without starting a Discord connection, the
questions that otherwise only surface as a confusing runtime error:
is the service account allowed in, do the tabs exist, does the roster
have duplicates the bot will refuse, and does gspread actually produce a
Sheets *checkbox* rather than the text "TRUE".

    .venv/bin/python items_preflight.py
    .venv/bin/python items_preflight.py --write-test "<IGN>" "<item name>"

The write test mutates a cell, so it refuses to run against the real
Logs Tracker: point ITEMS_SHEET_ID at a copy first. Everything else here
is read-only and safe against any sheet.
"""

import argparse
import os
import sys
import types

from dotenv import load_dotenv

import items_rules
import items_sheet
from attendance_roster import normalize

# The guild's real sheet. The write test refuses to touch it -- a
# stray tick in a hand-maintained tracker is exactly the kind of damage
# nobody notices until an officer disputes it weeks later.
PRODUCTION_SHEET_ID = "1Xx44UKBx0v5Pa0xbBzuVElEFZK-mdeQ5jHBBzBsKQgc"

OK = "  ok  "
WARN = " warn "
FAIL = " FAIL "


class PreflightFailure(RuntimeError):
    """Something is wrong that would stop the bot working."""


def line(status: str, message: str) -> None:
    print(f"[{status}] {message}", flush=True)


def check_env() -> dict:
    """Every credential the bot needs, or a clear list of what is absent."""
    missing = [name for name in items_sheet_required() if not os.getenv(name)]
    if missing:
        for name in missing:
            line(FAIL, f"{name} is not set")
        raise PreflightFailure(
            "Missing credentials. Add them to .env (see the setup notes), "
            "then run this again."
        )
    line(OK, "all three credentials are present")
    return {name: os.environ[name] for name in items_sheet_required()}


def items_sheet_required() -> tuple[str, ...]:
    return ("ITEMS_DISCORD_TOKEN", "ITEMS_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")


def check_access(sheet_id: str, service_account_json: str):
    try:
        spreadsheet = items_sheet.open_logs_tracker(sheet_id, service_account_json)
    except Exception as exc:
        line(FAIL, f"cannot open the spreadsheet: {exc}")
        raise PreflightFailure(
            "The service account cannot open this sheet. Share the sheet with "
            "the service account's client_email as an Editor."
        ) from None
    line(OK, f"opened spreadsheet {spreadsheet.title!r}")
    return spreadsheet


def check_tabs(spreadsheet) -> None:
    titles = [ws.title for ws in spreadsheet.worksheets()]
    line(OK, f"tabs found: {', '.join(titles)}")

    if items_sheet.SPECIAL_TAB not in titles:
        raise PreflightFailure(
            f"No {items_sheet.SPECIAL_TAB!r} tab. The bot cannot run without it -- "
            "it is the roster as well as the special-log record."
        )
    line(OK, f"{items_sheet.SPECIAL_TAB!r} present")

    if items_sheet.GEAR_TAB not in titles:
        line(
            WARN,
            f"{items_sheet.GEAR_TAB!r} is absent -- `!request` will be refused "
            "until you create it. Special-log raffles work fine meanwhile.",
        )
    else:
        line(OK, f"{items_sheet.GEAR_TAB!r} present")

    if items_sheet.LEDGER_TAB in titles:
        line(OK, f"{items_sheet.LEDGER_TAB!r} already exists")
    else:
        line(OK, f"{items_sheet.LEDGER_TAB!r} will be created on the first approval")


def check_snapshot(spreadsheet):
    try:
        snapshot = items_sheet.read_snapshot(spreadsheet)
    except Exception as exc:
        line(FAIL, f"cannot read the sheet: {exc}")
        raise PreflightFailure(str(exc)) from None

    line(OK, f"{len(snapshot.roster)} players in column A")
    specials = items_rules.item_names(snapshot.special_headers)
    gears = items_rules.item_names(snapshot.gear_headers)
    line(OK, f"{len(specials)} special-log items, {len(gears)} gear-log items")
    line(OK, f"{len(snapshot.ledger_rows)} rows already in the distribution log")
    return snapshot, specials, gears


def check_duplicate_players(snapshot) -> None:
    """Two rows that normalise alike make the bot refuse writes for both.

    Worth catching here rather than when an officer clicks approve and
    gets an error they cannot act on quickly.
    """
    seen: dict[str, str] = {}
    clashes: list[tuple[str, str]] = []
    for player in snapshot.roster:
        key = normalize(player)
        if key in seen:
            clashes.append((seen[key], player))
        else:
            seen[key] = player

    if clashes:
        for first, second in clashes:
            line(FAIL, f"duplicate player rows: {first!r} and {second!r}")
        raise PreflightFailure(
            "Two rows in column A mean the same player. The bot refuses to "
            "guess which row to write, so requests for them will fail. Remove "
            "or rename one row."
        )
    line(OK, "no duplicate player rows")


def check_item_clashes(specials: list[str], gears: list[str]) -> None:
    both = sorted({s for s in specials} & {g for g in gears})
    if both:
        for name in both:
            line(FAIL, f"{name!r} is a column in BOTH tabs")
        raise PreflightFailure(
            "An item in both tabs is ambiguous -- the bot refuses to guess "
            "which one an officer meant. Remove the duplicate column."
        )
    line(OK, "no item appears in both tabs")


def check_parses(snapshot, specials, gears) -> None:
    """Prove real command arguments resolve, using this sheet's own data.

    Two separate checks now, because the two item kinds go through
    different commands: gear logs through `!request`, special logs
    through the raffle's `!poll`. Feeding a special log to parse_request
    is EXPECTED to fail, so it is asserted rather than reported as a
    problem with the sheet.
    """
    if not snapshot.roster or not (specials or gears):
        line(WARN, "not enough data to test a sample command")
        return
    player = snapshot.roster[0]

    if gears:
        sample = f"{gears[0]} {player}"
        try:
            parsed = items_rules.parse_request(
                sample, snapshot.roster, snapshot.special_headers, snapshot.gear_headers
            )
        except Exception as exc:
            line(FAIL, f"a request built from your own sheet failed to parse: {exc}")
            raise PreflightFailure(str(exc)) from None
        line(OK, f"`!request {sample}` -> {parsed.item.name!r} ({parsed.item.type}) for {parsed.ign!r}")
    else:
        line(WARN, "no gear-log columns yet, so `!request` could not be tested")

    if not specials:
        line(WARN, "no special-log columns, so the raffle could not be tested")
        return

    special = specials[0]
    try:
        resolved = items_rules.resolve_special(
            special, snapshot.special_headers, snapshot.gear_headers
        )
    except Exception as exc:
        line(FAIL, f"a raffle built from your own sheet failed to resolve: {exc}")
        raise PreflightFailure(str(exc)) from None
    line(OK, f"`!poll {special}` -> {resolved!r}")

    try:
        items_rules.parse_request(
            f"{special} {player}",
            snapshot.roster,
            snapshot.special_headers,
            snapshot.gear_headers,
        )
    except items_rules.RequestParseError:
        line(OK, f"`!request {special} ...` is correctly refused (special logs are raffled)")
    else:
        line(FAIL, f"{special!r} is a special log but `!request` accepted it")
        raise PreflightFailure(f"{special!r} can still be requested")


def check_write(spreadsheet, sheet_id: str, ign: str, item: str) -> None:
    """Tick one real checkbox so a human can confirm it renders as a box."""
    if sheet_id == PRODUCTION_SHEET_ID:
        raise PreflightFailure(
            "Refusing to write to the real Logs Tracker. Make a copy, share it "
            "with the service account, point ITEMS_SHEET_ID at the copy, and "
            "run the write test there."
        )

    snapshot = items_sheet.read_snapshot(spreadsheet)
    player = items_rules.resolve_ign(ign, snapshot.roster)
    if player is None:
        raise PreflightFailure(f"No player named {ign!r} in the sheet.")

    # Special logs are resolved directly rather than through
    # parse_request, which refuses them now that they are raffled. The
    # checkbox write itself is unchanged -- it is what !winner performs.
    try:
        name = items_rules.resolve_special(
            item, snapshot.special_headers, snapshot.gear_headers
        )
        item_type = items_rules.SPECIAL
    except items_rules.ItemLookupError:
        resolved = items_rules.resolve_item(
            item, snapshot.special_headers, snapshot.gear_headers
        )
        name, item_type = resolved.name, resolved.type

    parsed = types.SimpleNamespace(
        ign=player, item=types.SimpleNamespace(name=name, type=item_type)
    )
    if item_type == items_rules.SPECIAL:
        address = items_sheet.record_special(spreadsheet, parsed.ign, parsed.item.name)
    else:
        address = items_sheet.record_gear(spreadsheet, parsed.ign, parsed.item.name)

    line(OK, f"wrote {parsed.item.name!r} for {parsed.ign!r} at cell {address}")
    print()
    print("  NOW LOOK AT THE SHEET. At cell", address, "you must see:")
    if parsed.item.type == items_rules.SPECIAL:
        print("    - a TICKED CHECKBOX  -> correct")
        print("    - the text 'TRUE' next to an empty box -> WRONG, tell Claude")
    else:
        print("    - the count incremented by exactly 1 -> correct")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-test",
        nargs=2,
        metavar=("IGN", "ITEM"),
        help="tick one real cell on a NON-production copy to confirm checkbox rendering",
    )
    args = parser.parse_args()

    load_dotenv()
    print("Item bot preflight\n" + "=" * 60)

    try:
        env = check_env()
        sheet_id = env["ITEMS_SHEET_ID"]
        if sheet_id == PRODUCTION_SHEET_ID:
            line(WARN, "ITEMS_SHEET_ID points at the REAL Logs Tracker")
        spreadsheet = check_access(sheet_id, env["GOOGLE_SERVICE_ACCOUNT_JSON"])
        check_tabs(spreadsheet)
        snapshot, specials, gears = check_snapshot(spreadsheet)
        check_duplicate_players(snapshot)
        check_item_clashes(specials, gears)
        check_parses(snapshot, specials, gears)

        if args.write_test:
            print("-" * 60)
            check_write(spreadsheet, sheet_id, args.write_test[0], args.write_test[1])
    except PreflightFailure as exc:
        print("=" * 60)
        print(f"PREFLIGHT FAILED: {exc}")
        return 1

    print("=" * 60)
    print("Preflight passed. You can start the bot:")
    print("    .venv/bin/python -u items_bot.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
