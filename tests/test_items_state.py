"""Tests for the pinned-message state format and queue operations."""

import items_state


def _request(request_id="aaa", ign="Kobe", item="Asta's Heart", type_="Special"):
    return items_state.PendingRequest(
        id=request_id,
        user_id=42,
        ign=ign,
        item=item,
        type=type_,
        requested_at="2026-08-07 09:00:00",
    )


def test_encode_decode_round_trip():
    state = items_state.State(
        officer_channel_id=99, queue=[_request()], igns={"42": "Kobe"}
    )
    content, dropped = items_state.encode_state(state)
    assert dropped == []

    restored = items_state.decode_state(content)
    assert restored.officer_channel_id == 99
    assert restored.igns == {"42": "Kobe"}
    assert restored.queue[0].ign == "Kobe"
    assert restored.queue[0].id == "aaa"


def test_encoded_content_carries_the_marker():
    content, _ = items_state.encode_state(items_state.State())
    assert content.startswith(items_state.STATE_MARKER)


def test_decoding_an_unrelated_message_returns_none():
    assert items_state.decode_state("just a normal chat message") is None


def test_decoding_a_corrupt_payload_returns_none():
    assert items_state.decode_state(f"{items_state.STATE_MARKER}\n```json\n{{oops\n```") is None


def test_decoding_a_non_numeric_channel_id_returns_none():
    content = f'{items_state.STATE_MARKER}\n```json\n{{"officer_channel_id":"bad"}}\n```'
    assert items_state.decode_state(content) is None


def test_a_fresh_state_has_no_officer_channel():
    assert items_state.State().officer_channel_id is None


def test_oversize_state_drops_the_oldest_requests_and_reports_them():
    many = [_request(request_id=f"id{n:03d}", item="A Very Long Item Name Indeed") for n in range(300)]
    state = items_state.State(officer_channel_id=99, queue=many)

    content, dropped = items_state.encode_state(state)

    assert len(content) <= items_state.MAX_CONTENT
    assert dropped, "oversize state must report what it dropped"
    assert dropped[0].id == "id000", "the OLDEST request is dropped first"
    assert items_state.decode_state(content).officer_channel_id == 99


def test_oversize_ign_memory_is_trimmed_after_the_queue_is_preserved():
    state = items_state.State(igns={str(n): "A very long remembered IGN value" for n in range(300)})
    content, dropped = items_state.encode_state(state)
    restored = items_state.decode_state(content)
    assert len(content) <= items_state.MAX_CONTENT
    assert dropped == []
    assert "0" not in state.igns
    assert "0" not in restored.igns
    assert "299" in restored.igns


def test_new_request_ids_are_unique():
    assert len({items_state.new_request_id() for _ in range(200)}) == 200


import items_rules


def test_pending_gear_counts_only_that_players_gear_requests():
    state = items_state.State(
        queue=[
            _request("a", ign="Kobe", type_=items_rules.GEAR),
            _request("b", ign="Kobe", type_=items_rules.GEAR),
            _request("c", ign="Kobe", type_=items_rules.SPECIAL),
            _request("d", ign="Dajz", type_=items_rules.GEAR),
        ]
    )
    assert items_state.pending_gear_for(state, "Kobe", "2026-08-07") == 2
    assert items_state.pending_gear_for(state, "kobe", "2026-08-07") == 2


def test_pending_gear_does_not_cross_the_pht_day_boundary():
    state = items_state.State(queue=[_request("a", ign="Kobe", type_=items_rules.GEAR)])
    assert items_state.pending_gear_for(state, "Kobe", "2026-08-08") == 0


def test_find_request_returns_none_when_absent():
    assert items_state.find_request(items_state.State(), "nope") is None


def test_remove_request_takes_it_out_of_the_queue():
    state = items_state.State(queue=[_request("a"), _request("b")])
    removed = items_state.remove_request(state, "a")
    assert removed.id == "a"
    assert [r.id for r in state.queue] == ["b"]


def test_removing_an_already_removed_request_returns_none():
    state = items_state.State(queue=[_request("a")])
    items_state.remove_request(state, "a")
    assert items_state.remove_request(state, "a") is None
