"""Tests for the member-facing pending-request board."""

import items_board
import items_state


def _request(ign="Kobe", item="Asta's Heart"):
    return items_state.PendingRequest(
        id="aaa",
        user_id=42,
        ign=ign,
        item=item,
        type="Special",
        requested_at="2026-08-07 09:00:00",
    )


def test_item_column_stays_aligned_for_differing_name_lengths():
    body = items_board.render_board(
        [_request("Odaiba", "Dark Orb Earrings"), _request("K", "Asta's Heart")]
    )

    rows = body.splitlines()[2:4]
    assert rows[0].index("Dark Orb Earrings") == rows[1].index("Asta's Heart")


def test_overlong_ign_and_item_are_visibly_truncated():
    body = items_board.render_board(
        [
            _request(
                "I" * (items_board.IGN_WIDTH + 1),
                "X" * (items_board.ITEM_WIDTH + 1),
            )
        ]
    )

    assert "I" * (items_board.IGN_WIDTH - 1) + "…" in body
    assert "X" * (items_board.ITEM_WIDTH - 1) + "…" in body


def test_board_limit_only_reports_requests_beyond_the_visible_rows():
    at_limit = items_board.render_board(
        [_request(ign=str(n)) for n in range(items_board.BOARD_LIMIT)]
    )
    over_limit = items_board.render_board(
        [_request(ign=str(n)) for n in range(items_board.BOARD_LIMIT + 1)]
    )

    assert "+" not in at_limit
    assert "+1 more waiting" in over_limit


def test_empty_queue_renders_a_nothing_pending_board():
    body = items_board.render_board([])

    assert "Nothing pending" in body
    assert "IGN" not in body


def test_positions_start_at_one_and_are_continuous():
    body = items_board.render_board([_request(ign=str(n)) for n in range(4)])

    assert [line[:2].strip() for line in body.splitlines()[2:6]] == [
        "1",
        "2",
        "3",
        "4",
    ]


def test_worst_case_visible_board_fits_an_embed_description():
    body = items_board.render_board(
        [
            _request("I" * items_board.IGN_WIDTH, "X" * items_board.ITEM_WIDTH)
            for _ in range(items_board.BOARD_LIMIT)
        ]
    )

    assert len(body) <= 4096


def test_backticks_in_queue_text_cannot_break_the_code_fence():
    body = items_board.render_board([_request("O`daiba", "Asta`s Heart")])

    assert body.count("`") == 6
    assert "O′daiba" in body
    assert "Asta′s Heart" in body


def test_whitespace_in_queue_text_stays_on_one_clean_row():
    body = items_board.render_board(
        [_request("\n Nor\nm\t\r ", "\t Item\nwith\ttabs\rand spaces ")]
    )

    assert "Nor m" in body
    assert "Item with tabs and spaces" in body
    row = body.splitlines()[2]
    assert row.startswith(" 1   Nor m")
    assert row.index("Item") == 22
    assert len(body.splitlines()) == 4
    assert body.count("```") == 2


def test_board_omits_officer_only_request_details():
    request = items_state.PendingRequest(
        id="aaa",
        user_id=987654321,
        ign="Kobe",
        item="Asta's Heart",
        type="Gear",
        requested_at="2026-08-07 09:00:00",
        note="previously requested as OtherKobe",
    )

    body = items_board.render_board([request])

    assert "Gear" not in body
    assert "previously requested" not in body
    assert "987654321" not in body
