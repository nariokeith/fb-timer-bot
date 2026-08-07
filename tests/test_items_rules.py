"""Tests for the pure allocation logic."""

from datetime import datetime

import items_rules


def test_pht_day_is_the_date_prefix_of_a_timestamp():
    assert items_rules.pht_day("2026-08-07 23:59:59") == "2026-08-07"


def test_one_minute_past_midnight_is_a_different_day():
    before = items_rules.pht_day("2026-08-07 23:59:00")
    after = items_rules.pht_day("2026-08-08 00:01:00")
    assert before != after


def test_format_timestamp_round_trips_through_pht_day():
    moment = datetime(2026, 8, 7, 23, 59, 0, tzinfo=items_rules.PHT)
    assert items_rules.pht_day(items_rules.format_timestamp(moment)) == "2026-08-07"


def test_now_pht_is_timezone_aware_in_manila():
    assert items_rules.now_pht().tzinfo is items_rules.PHT


LEDGER = [
    ["2026-08-07 09:00:00", "Kobe", "Asta's Belt", "Gear", "Officer", "1", "aaa"],
    ["2026-08-07 10:00:00", "Kobe", "Benji's Heart", "Gear", "Officer", "1", "bbb"],
    ["2026-08-07 11:00:00", "Dajz", "Benji's Heart", "Gear", "Officer", "2", "ccc"],
    ["2026-08-06 23:59:00", "Kobe", "Amentis' Foot", "Gear", "Officer", "1", "ddd"],
    ["2026-08-07 12:00:00", "Kobe", "Asta's Heart", "Special", "Officer", "1", "eee"],
]


def test_counts_only_this_players_gear_rows_from_today():
    assert items_rules.gear_used_today(LEDGER, "Kobe", "2026-08-07") == 2


def test_special_rows_never_count_against_the_gear_cap():
    only_special = [r for r in LEDGER if r[3] == "Special"]
    assert items_rules.gear_used_today(only_special, "Kobe", "2026-08-07") == 0


def test_yesterdays_rows_do_not_count():
    assert items_rules.gear_used_today(LEDGER, "Kobe", "2026-08-06") == 1


def test_ign_matching_ignores_case_and_spacing():
    assert items_rules.gear_used_today(LEDGER, "  kobe ", "2026-08-07") == 2


def test_short_rows_are_skipped_not_fatal():
    assert items_rules.gear_used_today([["2026-08-07 09:00:00"]], "Kobe", "2026-08-07") == 0
