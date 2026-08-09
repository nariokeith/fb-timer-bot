"""Tests for the raffle's pure logic.

No Discord, no Google Sheets, no clock. Everything here is decided from
values passed in, which is what makes the nickname rules testable.
"""

import pytest

import items_raffle

ROSTER = ["Jjew", "Kobe", "Ryuu", "chinchong ni Mumu", "wile-KAMOTE"]


@pytest.mark.parametrize(
    "nickname",
    ["Jjew", "BK | Jjew", "M2 | Jjew", "BK Jjew", "BK - Jjew", "  BK|Jjew  "],
)
def test_every_tag_format_resolves_to_the_bare_ign(nickname):
    assert items_raffle.resolve_voter(nickname, ROSTER) == "Jjew"


def test_a_multi_word_ign_survives_tag_stripping():
    assert (
        items_raffle.resolve_voter("M2 - chinchong ni Mumu", ROSTER)
        == "chinchong ni Mumu"
    )


def test_an_ign_containing_a_hyphen_is_not_split_apart():
    """The remainder is a slice of the original string, not re-joined tokens.

    Re-joining would have to reconstruct 'wile-KAMOTE' from ['wile',
    'KAMOTE'] and would silently produce 'wile KAMOTE', which is not a
    roster row.
    """
    assert items_raffle.resolve_voter("BK | wile-KAMOTE", ROSTER) == "wile-KAMOTE"


def test_an_alias_resolves_through_the_roster():
    assert items_raffle.resolve_voter("BK | KobePH", ROSTER) == "Kobe"


def test_a_tag_that_is_itself_a_roster_name_is_still_only_a_tag():
    """Candidates are suffixes, so the leading 'Kobe' can never win.

    The guild tag is always on the left. A member called Jjew whose tag
    happens to be another player's name still resolves to Jjew.
    """
    assert items_raffle.resolve_voter("Kobe | Jjew", ROSTER) == "Jjew"


def test_a_nickname_matching_two_different_rows_does_not_resolve():
    """Two roster rows where one is a suffix of the other. Refuse, don't pick.

    'BK | chinchong ni Mumu' produces both 'chinchong ni Mumu' and
    'ni Mumu' as candidates. If the sheet ever holds both, the bot has
    no way to tell which player is meant.
    """
    roster = ROSTER + ["ni Mumu"]

    assert items_raffle.resolve_voter("BK | chinchong ni Mumu", roster) is None


def test_an_unknown_nickname_does_not_resolve():
    assert items_raffle.resolve_voter("BK | Nobody", ROSTER) is None


def test_an_empty_nickname_does_not_resolve():
    assert items_raffle.resolve_voter("   ", ROSTER) is None


def test_candidates_include_the_whole_nickname_first():
    assert items_raffle.nickname_candidates("BK | Jjew")[0] == "BK | Jjew"


def test_candidates_do_not_repeat():
    candidates = items_raffle.nickname_candidates("BK  |  Jjew")
    assert len(candidates) == len(set(candidates))


def _voter(user_id, display_name):
    return items_raffle.Voter(user_id=user_id, display_name=display_name)


def test_voters_split_into_eligible_already_have_and_unidentified():
    voters = [
        _voter(1, "BK | Jjew"),
        _voter(2, "M2 - Kobe"),
        _voter(3, "BK | Nobody"),
    ]
    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: ign == "Kobe"
    )

    assert split.eligible == ["Jjew"]
    assert split.already_have == ["Kobe"]
    assert [v.user_id for v in split.unidentified] == [3]


def test_the_same_player_voting_from_two_accounts_is_listed_once():
    voters = [_voter(1, "BK | Jjew"), _voter(2, "Jjew")]
    split = items_raffle.classify_voters(voters, ROSTER, holds=lambda ign: False)

    assert split.eligible == ["Jjew"]


def test_eligibility_keeps_the_order_players_voted_in():
    voters = [_voter(1, "Ryuu"), _voter(2, "BK | Jjew"), _voter(3, "Kobe")]
    split = items_raffle.classify_voters(voters, ROSTER, holds=lambda ign: False)

    assert split.eligible == ["Ryuu", "Jjew", "Kobe"]


def test_no_voters_gives_three_empty_groups():
    split = items_raffle.classify_voters([], ROSTER, holds=lambda ign: False)

    assert split.eligible == []
    assert split.already_have == []
    assert split.unidentified == []


def test_a_poll_argument_without_a_flag_uses_the_default_duration():
    parsed = items_raffle.parse_poll_argument("Asta's Heart")

    assert parsed.item_query == "Asta's Heart"
    assert parsed.hours == items_raffle.DEFAULT_POLL_HOURS


def test_the_hours_flag_overrides_the_duration_and_is_stripped():
    parsed = items_raffle.parse_poll_argument("Asta's Heart --hours 48")

    assert parsed.item_query == "Asta's Heart"
    assert parsed.hours == 48


def test_an_hours_flag_without_a_number_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="--hours"):
        items_raffle.parse_poll_argument("Asta's Heart --hours")


def test_a_non_numeric_hours_value_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="whole number"):
        items_raffle.parse_poll_argument("Asta's Heart --hours banana")


@pytest.mark.parametrize("hours", [0, 169])
def test_an_out_of_range_duration_is_refused(hours):
    with pytest.raises(items_raffle.RaffleArgumentError, match="between 1 and 168"):
        items_raffle.parse_poll_argument(f"Asta's Heart --hours {hours}")


def test_an_empty_poll_argument_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Usage"):
        items_raffle.parse_poll_argument("   ")


def test_a_poll_argument_that_is_only_a_flag_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Usage"):
        items_raffle.parse_poll_argument("--hours 48")


def test_winner_splits_a_multi_word_item_from_a_multi_word_ign():
    item, ign = items_raffle.split_item_and_ign(
        "Asta's Heart chinchong ni Mumu", ["Asta's Heart"], ROSTER
    )

    assert (item, ign) == ("Asta's Heart", "chinchong ni Mumu")


def test_winner_resolves_the_ign_through_an_alias():
    item, ign = items_raffle.split_item_and_ign(
        "Asta's Heart KobePH", ["Asta's Heart"], ROSTER
    )

    assert (item, ign) == ("Asta's Heart", "Kobe")


def test_winner_refuses_an_unknown_item():
    with pytest.raises(items_raffle.RaffleArgumentError, match="No open raffle"):
        items_raffle.split_item_and_ign("Benji's Heart Jjew", ["Asta's Heart"], ROSTER)


def test_winner_refuses_an_unknown_player():
    with pytest.raises(items_raffle.RaffleArgumentError, match="No player named"):
        items_raffle.split_item_and_ign(
            "Asta's Heart Nobody", ["Asta's Heart"], ROSTER
        )


def test_winner_refuses_a_single_word_argument():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Usage"):
        items_raffle.split_item_and_ign("Asta's", ["Asta's Heart"], ROSTER)


def test_winner_refuses_an_argument_that_reads_two_ways():
    """'Kobe' is both a raffle name and a player here. Refuse, don't pick."""
    with pytest.raises(items_raffle.RaffleArgumentError, match="more than one way"):
        items_raffle.split_item_and_ign("Kobe Kobe Jjew", ["Kobe", "Kobe Kobe"], ROSTER + ["Kobe Jjew"])
