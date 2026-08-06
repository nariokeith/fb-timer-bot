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
    # Direct assertion: normalize must preserve the non-ASCII codepoint.
    assert "卐" in normalize("wileKAMOTE卐")

    # Match also works; if normalize stripped non-ASCII, this would fail.
    matched, unmatched = match_names(["wileKAMOTE卐"], KNOWN)
    assert _players(matched) == ["wileKAMOTE卐"]
    assert unmatched == []


def test_non_ascii_collision_if_stripped():
    # Two known players differ ONLY in non-ASCII: stripping would
    # collapse them into one, breaking the match or creating ambiguity.
    known_with_variants = ["Kobe", "Kobe⚡"]
    matched, unmatched = match_names(["Kobe"], known_with_variants)
    assert _players(matched) == ["Kobe"]
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
    # Test exact-match dedup (case/whitespace variants).
    matched, unmatched = match_names(["Kobe", "kobe", "  KOBE"], KNOWN)
    assert _players(matched) == ["Kobe"]
    assert unmatched == []


def test_dedup_across_fuzzy_matches():
    # Two different raw strings, both fuzzy-matching to the same known player.
    # Dedup must happen by RESOLVED PLAYER, not by normalized raw string.
    # "ToastdBread" (single-char omission) and "ToastedBrea" (single-char omission)
    # both score ~0.917 above the 0.85 threshold, but resolve to the same player.
    matched, unmatched = match_names(
        ["ToastdBread", "ToastedBrea"], ["ToastedBread"]
    )
    # Only one Match should be produced; the second is deduplicated.
    assert len(matched) == 1
    assert matched[0].player == "ToastedBread"
    # Verify the match score reflects fuzzy matching, not exact.
    assert 0.85 <= matched[0].score < 1.0
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


def test_alias_resolves_with_score_1_0():
    # KobePH is an alias for Kobe; should resolve with score 1.0.
    matched, unmatched = match_names(["KobePH"], KNOWN)
    assert _players(matched) == ["Kobe"]
    assert matched[0].score == 1.0
    assert unmatched == []


def test_alias_matching_ignores_case_and_whitespace():
    # Aliases are matched on normalized form.
    matched, unmatched = match_names(["  kobeph  ", "KOBEPH"], KNOWN)
    assert _players(matched) == ["Kobe"]
    assert len(matched) == 1  # Both map to the same player, so dedup.
    assert unmatched == []


def test_alias_with_missing_target_is_unmatched():
    # If the sheet has no "Kobe" row, KobePH must not invent one.
    known_without_kobe = [
        "ARCILynN", "xSigarilyas", "Talong", "XxLINGAxX", "fLuffy",
        "ToastedBread", "wileKAMOTE卐", "BudoySul (Riuz)", "chinchong ni Mumu",
    ]
    matched, unmatched = match_names(["KobePH"], known_without_kobe)
    assert matched == []
    assert unmatched == ["KobePH"]


def test_exact_match_wins_over_alias():
    # If a sheet row is literally named "KobePH", it wins over the alias.
    known_with_kobeph = KNOWN + ["KobePH"]
    matched, unmatched = match_names(["KobePH"], known_with_kobeph)
    assert _players(matched) == ["KobePH"]
    assert matched[0].score == 1.0
    assert unmatched == []


def test_dedup_across_alias_and_exact_match():
    # Reading both "Kobe" and "KobePH" off one screenshot produces one Match.
    matched, unmatched = match_names(["Kobe", "KobePH"], KNOWN)
    assert len(matched) == 1
    assert matched[0].player == "Kobe"
    assert unmatched == []


def test_japanese_alias_resolves():
    # 面白い (in-game) is an alias for chinchong ni Mumu (sheet row).
    # In-game Japanese name shares no characters with the Filipino nickname.
    matched, unmatched = match_names(["面白い"], KNOWN)
    assert _players(matched) == ["chinchong ni Mumu"]
    assert matched[0].score == 1.0
    assert unmatched == []


def test_alias_resolves_with_zero_similarity():
    # Some aliases target sheet rows with zero character overlap.
    # This test proves aliases work even when SequenceMatcher scores 0.0.
    # This is critical: it documents why aliases cannot be replaced by
    # threshold tuning, and it will fail loudly if someone tries to "simplify"
    # by removing aliases in favour of fuzzy matching.
    from difflib import SequenceMatcher

    # Verify the similarity is indeed 0.0 (no shared characters).
    score = SequenceMatcher(None, normalize("面白い"), normalize("chinchong ni mumu")).ratio()
    assert score < 0.85, f"Expected similarity < 0.85, got {score} (test precondition failed)"

    # But the alias still resolves with score 1.0 (definite match).
    matched, unmatched = match_names(["面白い"], KNOWN)
    assert len(matched) == 1
    assert matched[0].player == "chinchong ni Mumu"
    assert matched[0].score == 1.0
    assert unmatched == []
