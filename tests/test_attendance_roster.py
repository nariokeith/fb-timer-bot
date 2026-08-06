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
