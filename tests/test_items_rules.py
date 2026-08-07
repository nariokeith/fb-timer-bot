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


import pytest

SPECIAL_HEADERS = ["Player Name", "Asta's Heart", "Amentis' Foot", "Benji's Blood"]
GEAR_HEADERS = ["Player Name", "Asta's Belt", "Benji's Heart"]


def test_resolves_an_item_to_the_tab_that_holds_it():
    found = items_rules.resolve_item("Asta's Heart", SPECIAL_HEADERS, GEAR_HEADERS)
    assert (found.name, found.type) == ("Asta's Heart", items_rules.SPECIAL)


def test_resolves_a_gear_item():
    found = items_rules.resolve_item("Asta's Belt", SPECIAL_HEADERS, GEAR_HEADERS)
    assert (found.name, found.type) == ("Asta's Belt", items_rules.GEAR)


def test_matching_ignores_case():
    found = items_rules.resolve_item("asta's belt", SPECIAL_HEADERS, GEAR_HEADERS)
    assert found.name == "Asta's Belt"


def test_a_partial_item_name_is_refused_but_lists_the_matches():
    """A member who types 'Asta' should be shown the Asta items.

    difflib scores 'Asta' against "Asta's Heart" at 0.5, so this only
    works because _suggest searches substrings before close matches.
    """
    with pytest.raises(items_rules.ItemLookupError) as exc:
        items_rules.resolve_item("Asta", SPECIAL_HEADERS, GEAR_HEADERS)
    assert "Asta's Heart" in str(exc.value)


def test_a_typo_is_refused_but_offers_the_close_match():
    """No substring overlap here -- this is the close-match path."""
    with pytest.raises(items_rules.ItemLookupError) as exc:
        items_rules.resolve_item("Astas Hesrt", SPECIAL_HEADERS, GEAR_HEADERS)
    assert "Asta's Heart" in str(exc.value)


def test_an_item_resembling_nothing_gets_no_suggestions():
    with pytest.raises(items_rules.ItemLookupError) as exc:
        items_rules.resolve_item("zzzzzzzz", SPECIAL_HEADERS, GEAR_HEADERS)
    assert "Did you mean" not in str(exc.value)


def test_an_item_in_both_tabs_is_refused_rather_than_guessed():
    with pytest.raises(items_rules.ItemLookupError) as exc:
        items_rules.resolve_item("Shared", ["Player Name", "Shared"], ["Player Name", "Shared"])
    assert "both" in str(exc.value).lower()


def test_the_player_name_column_is_never_treated_as_an_item():
    with pytest.raises(items_rules.ItemLookupError):
        items_rules.resolve_item("Player Name", SPECIAL_HEADERS, GEAR_HEADERS)


def test_a_blank_query_is_refused():
    with pytest.raises(items_rules.ItemLookupError):
        items_rules.resolve_item("   ", SPECIAL_HEADERS, GEAR_HEADERS)


ROSTER = ["Kobe", "Dajz", "chinchong ni Mumu", "Smth"]


def _parse(argument):
    return items_rules.parse_request(argument, ROSTER, SPECIAL_HEADERS, GEAR_HEADERS)


def test_parses_a_single_word_ign():
    parsed = _parse("Asta's Heart Kobe")
    assert (parsed.item.name, parsed.ign) == ("Asta's Heart", "Kobe")


def test_parses_an_ign_containing_spaces():
    parsed = _parse("Asta's Heart chinchong ni Mumu")
    assert (parsed.item.name, parsed.ign) == ("Asta's Heart", "chinchong ni Mumu")


def test_parses_a_gear_item():
    parsed = _parse("Asta's Belt Dajz")
    assert (parsed.item.name, parsed.item.type) == ("Asta's Belt", items_rules.GEAR)


def test_extra_whitespace_does_not_matter():
    parsed = _parse("  Asta's   Heart    Kobe ")
    assert parsed.ign == "Kobe"


def test_an_unknown_ign_is_refused_and_named():
    with pytest.raises(items_rules.RequestParseError) as exc:
        _parse("Asta's Heart Kobee")
    assert "Kobee" in str(exc.value) or "Kobe" in str(exc.value)


def test_an_unknown_item_is_refused():
    with pytest.raises(items_rules.RequestParseError):
        _parse("Nonexistent Thing Kobe")


def test_a_bare_ign_with_no_item_is_refused():
    with pytest.raises(items_rules.RequestParseError):
        _parse("Kobe")


def test_an_empty_argument_is_refused():
    with pytest.raises(items_rules.RequestParseError):
        _parse("")


def test_an_alias_resolves_to_the_sheet_row():
    """'ツRyuuツ' shares no Latin characters with 'Ryuu'.

    Aliases are the only possible resolution path for names like this --
    no fuzzy threshold, however low, could ever match them.
    """
    parsed = items_rules.parse_request(
        "Asta's Heart ツRyuuツ", ["Ryuu"], SPECIAL_HEADERS, GEAR_HEADERS
    )
    assert parsed.ign == "Ryuu"


def test_an_alias_whose_target_is_absent_does_not_resolve():
    with pytest.raises(items_rules.RequestParseError):
        items_rules.parse_request(
            "Asta's Heart ツRyuuツ", ["Kobe"], SPECIAL_HEADERS, GEAR_HEADERS
        )


def test_a_special_the_player_lacks_is_allowed():
    result = items_rules.check_eligibility(
        items_rules.SPECIAL, "Kobe", [], "2026-08-07", already_has_special=False
    )
    assert result.allowed


def test_a_special_the_player_already_has_is_refused():
    result = items_rules.check_eligibility(
        items_rules.SPECIAL, "Kobe", [], "2026-08-07", already_has_special=True
    )
    assert not result.allowed
    assert "already" in result.reason.lower()


def test_gear_under_the_cap_is_allowed():
    result = items_rules.check_eligibility(
        items_rules.GEAR, "Kobe", LEDGER, "2026-08-07", already_has_special=False
    )
    assert result.allowed
    assert (result.used, result.cap) == (2, 3)


def test_gear_at_the_cap_is_refused():
    ledger = LEDGER + [
        ["2026-08-07 13:00:00", "Kobe", "Asta's Belt", "Gear", "O", "1", "fff"]
    ]
    result = items_rules.check_eligibility(
        items_rules.GEAR, "Kobe", ledger, "2026-08-07", already_has_special=False
    )
    assert not result.allowed
    assert "3/3" in result.reason


def test_pending_requests_count_toward_the_cap():
    result = items_rules.check_eligibility(
        items_rules.GEAR,
        "Kobe",
        LEDGER,
        "2026-08-07",
        already_has_special=False,
        pending_gear=1,
    )
    assert not result.allowed


def test_the_cap_is_configurable():
    result = items_rules.check_eligibility(
        items_rules.GEAR, "Kobe", LEDGER, "2026-08-07", already_has_special=False, cap=2
    )
    assert not result.allowed
