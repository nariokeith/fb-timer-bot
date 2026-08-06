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


def test_substring_match_resolves_when_prefix_does_not():
    # "dal" is not a prefix of "Lady Dalia", but it is a substring.
    # It should resolve via the substring fallback stage.
    assert resolve_boss(HEADERS, "dal") == "Lady Dalia"
    # But "dalia" (the full last part) also works via substring.
    assert resolve_boss(HEADERS, "dalia") == "Lady Dalia"


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


def test_prefix_beats_substring():
    # When one column starts with the query and another merely contains it,
    # the prefix one should win without ambiguity.
    headers = ["Player Name", "Points", "Ordo", "Lady Ordo"]
    assert resolve_boss(headers, "ordo") == "Ordo"


def test_ambiguous_substring_raises():
    # Multiple columns containing the substring should raise BossAmbiguous.
    # Use "aj" which appears in both "Rakajeth" and "Tajah" but doesn't start either.
    headers = ["Player Name", "Points", "Rakajeth", "Tajah"]
    with pytest.raises(BossAmbiguous) as excinfo:
        resolve_boss(headers, "aj")
    assert "Rakajeth" in str(excinfo.value)
    assert "Tajah" in str(excinfo.value)


def test_structural_columns_excluded_from_substring_match():
    # "play" should not resolve to "Player Name" even via substring.
    with pytest.raises(BossNotFound):
        resolve_boss(HEADERS, "play")


def test_missing_boss_still_raises():
    # A boss with no attendance column should still raise BossNotFound.
    with pytest.raises(BossNotFound):
        resolve_boss(HEADERS, "venatus")


def test_exact_match_wins_over_substring():
    # If there's an exact match, it should win even if other substring matches exist.
    headers = ["Player Name", "Points", "EGO", "Ego Whip"]
    assert resolve_boss(headers, "ego") == "EGO"
