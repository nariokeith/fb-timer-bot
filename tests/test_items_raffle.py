"""Tests for the raffle's pure logic.

No Discord, no Google Sheets, no clock. Everything here is decided from
values passed in, which is what makes the nickname rules testable.
"""

import pytest

import items_raffle

ROSTER = ["Jjew", "Kobe", "Ryuu", "chinchong ni Mumu", "wile-KAMOTE"]


def test_split_igns_reads_one_name():
    assert items_raffle.split_igns("Jjew", ROSTER) == ["Jjew"]


def test_split_igns_reads_several_names():
    assert items_raffle.split_igns("Jjew - Kobe", ROSTER) == ["Jjew", "Kobe"]


def test_split_igns_keeps_a_hyphenated_name_intact():
    """A separator needs whitespace on BOTH sides; 'wile-KAMOTE' is a row."""
    assert items_raffle.split_igns("wile-KAMOTE", ROSTER) == ["wile-KAMOTE"]
    assert items_raffle.split_igns("wile-KAMOTE - Jjew", ROSTER) == [
        "wile-KAMOTE",
        "Jjew",
    ]


def test_split_igns_keeps_a_multi_word_name_intact():
    assert items_raffle.split_igns("chinchong ni Mumu", ROSTER) == [
        "chinchong ni Mumu"
    ]


def test_split_igns_returns_the_roster_spelling():
    assert items_raffle.split_igns("jjew", ROSTER) == ["Jjew"]


def test_split_igns_refuses_an_empty_argument():
    with pytest.raises(items_raffle.RaffleArgumentError):
        items_raffle.split_igns("   ", ROSTER)


def test_split_igns_refuses_a_dangling_separator():
    with pytest.raises(items_raffle.RaffleArgumentError, match="empty name"):
        items_raffle.split_igns("Jjew - ", ROSTER)


def test_split_igns_refuses_an_unknown_name_with_a_suggestion():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Did you mean"):
        items_raffle.split_igns("Jjeww", ROSTER)


def test_split_igns_refuses_the_same_player_twice():
    with pytest.raises(items_raffle.RaffleArgumentError, match="more than once"):
        items_raffle.split_igns("Jjew - jjew", ROSTER)


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
        voters, ROSTER, holds=lambda ign: ign == "Kobe", identities=_identities()
    )

    assert split.eligible == ["Jjew"]
    assert split.already_have == ["Kobe"]
    assert [v.user_id for v in split.unidentified] == [3]


def test_the_same_player_voting_from_two_accounts_is_listed_once():
    voters = [_voter(1, "BK | Jjew"), _voter(2, "Jjew")]
    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: False, identities=_identities()
    )

    assert split.eligible == ["Jjew"]


def test_eligibility_keeps_the_order_players_voted_in():
    voters = [_voter(1, "Ryuu"), _voter(2, "BK | Jjew"), _voter(3, "Kobe")]
    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: False, identities=_identities()
    )

    assert split.eligible == ["Ryuu", "Jjew", "Kobe"]


def test_no_voters_gives_three_empty_groups():
    split = items_raffle.classify_voters(
        [], ROSTER, holds=lambda ign: False, identities=_identities()
    )

    assert split.eligible == []
    assert split.already_have == []
    assert split.unidentified == []


def test_remaining_pool_removes_this_sessions_winners():
    pool, excluded = items_raffle.remaining_pool(
        ["Jjew", "Kobe", "wile-KAMOTE"], ["Kobe"]
    )

    assert pool == ["Jjew", "wile-KAMOTE"]
    assert excluded == ["Kobe"]


def test_remaining_pool_keeps_the_order_of_the_frozen_list():
    pool, _ = items_raffle.remaining_pool(["Kobe", "Jjew"], [])

    assert pool == ["Kobe", "Jjew"]


def test_remaining_pool_matches_an_alias_not_the_raw_string():
    """A differently-spelled winner must not slip back into a later pool."""
    pool, excluded = items_raffle.remaining_pool(["Jjew", "Kobe"], ["  jjew "])

    assert pool == ["Kobe"]
    assert excluded == ["Jjew"]


def test_remaining_pool_with_no_winners_yet_changes_nothing():
    pool, excluded = items_raffle.remaining_pool(["Jjew", "Kobe"], [])

    assert pool == ["Jjew", "Kobe"]
    assert excluded == []


def test_remaining_pool_can_empty_the_pool():
    pool, excluded = items_raffle.remaining_pool(["Jjew"], ["Jjew"])

    assert pool == []
    assert excluded == ["Jjew"]


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


ITEMS = ["Asta's Heart", "Amentis Foot"]


def test_one_winner_still_parses_with_no_dash():
    assert items_raffle.split_item_and_igns("Asta's Heart Jjew", ITEMS, ROSTER) == (
        "Asta's Heart",
        ["Jjew"],
    )


def test_the_item_may_run_into_the_first_winner():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot Jjew - Kobe - Ryuu", ITEMS, ROSTER
    ) == ("Amentis Foot", ["Jjew", "Kobe", "Ryuu"])


def test_the_item_may_be_followed_by_its_own_dash():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot - Jjew - Kobe", ITEMS, ROSTER
    ) == ("Amentis Foot", ["Jjew", "Kobe"])


def test_a_hyphenated_ign_is_not_split_into_two_winners():
    """'wile-KAMOTE' has no space around its hyphen, so it is one name."""
    assert items_raffle.split_item_and_igns(
        "Amentis Foot wile-KAMOTE - Kobe", ITEMS, ROSTER
    ) == ("Amentis Foot", ["wile-KAMOTE", "Kobe"])


def test_a_multi_word_ign_survives_the_split():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot chinchong ni Mumu - Kobe", ITEMS, ROSTER
    ) == ("Amentis Foot", ["chinchong ni Mumu", "Kobe"])


def test_extra_spaces_around_the_dash_are_tolerated():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot Jjew   -   Kobe", ITEMS, ROSTER
    ) == ("Amentis Foot", ["Jjew", "Kobe"])


def test_an_alias_resolves_in_a_later_position():
    assert items_raffle.split_item_and_igns(
        "Amentis Foot Jjew - KobePH", ITEMS, ROSTER
    ) == ("Amentis Foot", ["Jjew", "Kobe"])


def test_the_same_player_twice_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="more than once"):
        items_raffle.split_item_and_igns("Amentis Foot Jjew - Jjew", ITEMS, ROSTER)


def test_an_alias_colliding_with_a_real_name_counts_as_a_duplicate():
    with pytest.raises(items_raffle.RaffleArgumentError, match="more than once"):
        items_raffle.split_item_and_igns("Amentis Foot Kobe - KobePH", ITEMS, ROSTER)


def test_a_trailing_dash_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="empty"):
        items_raffle.split_item_and_igns("Amentis Foot Jjew - ", ITEMS, ROSTER)


def _identities(bindings=None, not_players=(), request_igns=None):
    return items_raffle.Identities(
        bindings=bindings or {},
        not_players=frozenset(not_players),
        request_igns=request_igns or {},
    )


def test_a_collapsed_duplicate_is_reported_not_dropped():
    """The pool is right either way, but nothing may vanish unmentioned."""
    voters = [_voter(1, "BK | Jjew"), _voter(2, "xXshadowXx")]
    identities = _identities(request_igns={"2": "Jjew"})

    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: False, identities=identities
    )

    assert split.eligible == ["Jjew"]
    assert [(v.user_id, ign) for v, ign in split.duplicates] == [(2, "Jjew")]


def test_a_duplicate_from_a_binding_is_reported_too():
    voters = [_voter(1, "BK | Jjew"), _voter(2, "xXshadowXx")]
    identities = _identities(bindings={"2": "Jjew"})

    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: False, identities=identities
    )

    assert split.eligible == ["Jjew"]
    assert [(v.user_id, ign) for v, ign in split.duplicates] == [(2, "Jjew")]


def test_a_binding_resolves_a_nickname_nothing_else_could():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(bindings={"7": "Kobe"})
    )

    assert (ign, source) == ("Kobe", "binding")


def test_a_binding_beats_a_nickname_that_would_resolve_differently():
    """This is how an officer corrects a wrong nickname match."""
    ign, source = items_raffle.resolve_identity(
        _voter(7, "BK | Jjew"), ROSTER, _identities(bindings={"7": "Kobe"})
    )

    assert (ign, source) == ("Kobe", "binding")


def test_a_not_a_player_voter_is_skipped():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(not_players=["7"])
    )

    assert (ign, source) == (None, "skipped")


def test_a_nickname_still_resolves_when_nothing_is_bound():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "BK | Jjew"), ROSTER, _identities()
    )

    assert (ign, source) == ("Jjew", "nickname")


def test_the_last_request_ign_is_the_fallback():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(request_igns={"7": "Ryuu"})
    )

    assert (ign, source) == ("Ryuu", "request")


def test_a_nickname_beats_the_request_fallback():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "BK | Jjew"), ROSTER, _identities(request_igns={"7": "Ryuu"})
    )

    assert (ign, source) == ("Jjew", "nickname")


def test_a_binding_naming_a_row_no_longer_in_the_roster_is_unresolved():
    """A player removed from the sheet must not stay drawable."""
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(bindings={"7": "LeftTheGuild"})
    )

    assert (ign, source) == (None, None)


def test_a_request_fallback_naming_a_missing_row_is_unresolved():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities(request_igns={"7": "LeftTheGuild"})
    )

    assert (ign, source) == (None, None)


def test_an_unresolvable_voter_is_unidentified():
    ign, source = items_raffle.resolve_identity(
        _voter(7, "xXshadowXx"), ROSTER, _identities()
    )

    assert (ign, source) == (None, None)


def test_classify_splits_every_group():
    voters = [
        _voter(1, "BK | Jjew"),          # nickname
        _voter(2, "xXshadowXx"),         # binding -> Kobe
        _voter(3, "who even"),           # request -> Ryuu
        _voter(4, "a guest"),            # skipped
        _voter(5, "nobody at all"),      # unidentified
    ]
    identities = _identities(
        bindings={"2": "Kobe"}, not_players=["4"], request_igns={"3": "Ryuu"}
    )

    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: ign == "Ryuu", identities=identities
    )

    assert split.eligible == ["Jjew", "Kobe"]
    assert split.already_have == ["Ryuu"]
    assert [v.user_id for v in split.skipped] == [4]
    assert [v.user_id for v in split.unidentified] == [5]
    assert [(v.user_id, ign) for v, ign in split.from_request] == [(3, "Ryuu")]


def test_a_duplicate_across_two_accounts_is_still_collapsed():
    """One player voting from an alt account must not double their odds."""
    voters = [_voter(1, "BK | Jjew"), _voter(2, "xXshadowXx")]
    identities = _identities(bindings={"2": "Jjew"})

    split = items_raffle.classify_voters(
        voters, ROSTER, holds=lambda ign: False, identities=identities
    )

    assert split.eligible == ["Jjew"]


def test_a_winner_whose_name_ends_in_a_hyphen_is_not_read_as_a_dangling_dash():
    """Only a hyphen with whitespace before it is a dangling separator."""
    roster = ["Jjew", "Kobe", "KAMOTE-"]

    assert items_raffle.split_item_and_igns(
        "Amentis Foot Kobe - KAMOTE-", ["Amentis Foot"], roster
    ) == ("Amentis Foot", ["Kobe", "KAMOTE-"])


def test_an_unknown_name_in_a_later_position_is_refused_by_name():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Nobody"):
        items_raffle.split_item_and_igns("Amentis Foot Jjew - Nobody", ITEMS, ROSTER)


def test_an_item_with_no_winner_at_all_is_refused():
    with pytest.raises(items_raffle.RaffleArgumentError, match="Which player"):
        items_raffle.split_item_and_igns("Amentis Foot", ITEMS, ROSTER)
