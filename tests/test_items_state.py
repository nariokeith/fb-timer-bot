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
    contents = items_state.encode_state(state)
    assert len(contents) == 1

    restored = items_state.decode_shards(contents)
    assert restored.officer_channel_id == 99
    assert restored.igns == {"42": "Kobe"}
    assert restored.queue[0].ign == "Kobe"
    assert restored.queue[0].id == "aaa"


def test_encoded_content_carries_the_marker():
    contents = items_state.encode_state(items_state.State())
    assert contents[0].startswith(items_state.STATE_MARKER)


def test_decoding_an_unrelated_message_returns_none():
    assert items_state.decode_state("just a normal chat message") is None


def test_decoding_a_corrupt_payload_returns_none():
    assert items_state.decode_state(f"{items_state.STATE_MARKER}\n```json\n{{oops\n```") is None


def test_decoding_a_non_numeric_channel_id_returns_none():
    content = f'{items_state.STATE_MARKER}\n```json\n{{"officer_channel_id":"bad"}}\n```'
    assert items_state.decode_state(content) is None


def test_a_fresh_state_has_no_officer_channel():
    assert items_state.State().officer_channel_id is None


def _remembered_igns(count):
    return {str(10**17 + n): "PlayerName%02d" % n for n in range(count)}


def test_200_remembered_igns_and_queue_round_trip_across_shards():
    state = items_state.State(
        officer_channel_id=10**17,
        igns=_remembered_igns(200),
        queue=[_request(request_id=f"id{n:03d}", ign=f"Member {n}") for n in range(5)],
    )

    contents = items_state.encode_state(state)
    restored = items_state.decode_shards(contents)

    assert restored.igns == state.igns
    assert restored.queue == state.queue
    assert all(len(content) <= items_state.MAX_CONTENT for content in contents)


def test_fits_when_igns_span_multiple_shards_within_the_limit():
    state = items_state.State(igns=_remembered_igns(200))

    assert len(items_state.encode_state(state)) > 1
    assert len(items_state.encode_state(state)) <= items_state.MAX_SHARDS
    assert items_state.fits(state)


def test_empty_queue_with_a_huge_ign_map_still_fits():
    state = items_state.State(igns=_remembered_igns(500))

    contents = items_state.encode_state(state)

    assert items_state.fits(state)
    assert all(len(content) <= items_state.MAX_CONTENT for content in contents)


def test_a_single_request_too_large_for_one_shard_still_raises_value_error():
    state = items_state.State(queue=[_request(item="X" * items_state.MAX_CONTENT)])

    try:
        items_state.encode_state(state)
    except ValueError as error:
        assert "pending request" in str(error)
    else:
        raise AssertionError("an unencodable request must be reported")


def test_large_queue_and_ign_map_round_trip_without_reordering_requests():
    state = items_state.State(
        officer_channel_id=99,
        queue=[_request(request_id=f"id{n:03d}", ign=f"Member {n}") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.officer_channel_id == 99
    assert restored.igns == state.igns
    assert [request.id for request in restored.queue] == [request.id for request in state.queue]


def test_each_encoded_shard_respects_discords_content_limit():
    state = items_state.State(
        officer_channel_id=99,
        queue=[_request(request_id=f"id{n:03d}", item="A Very Long Item Name Indeed") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )

    assert all(len(content) <= items_state.MAX_CONTENT for content in items_state.encode_state(state))


def test_old_single_message_format_decodes_as_one_shard():
    content = """ITEMS_STATE_V1 -- bot storage, please don't delete this message.
```json
{"officer_channel_id":99,"queue":[{"id":"old1","user_id":42,"ign":"Kobe","item":"Asta's Heart","type":"Special","requested_at":"2026-08-07 09:00:00","note":""}],"igns":{"42":"Kobe"}}
```"""

    shard = items_state.decode_state(content)

    assert shard.part == 0
    assert shard.total == 1
    assert shard.state.officer_channel_id == 99
    assert [request.id for request in shard.state.queue] == ["old1"]


def test_decode_shards_reassembles_out_of_order_queue_slices():
    state = items_state.State(
        queue=[_request(request_id=f"id{n:03d}") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )
    contents = items_state.encode_state(state)
    assert len(contents) > 1

    restored = items_state.decode_shards(list(reversed(contents)))

    assert [request.id for request in restored.queue] == [request.id for request in state.queue]


def test_decode_shards_reports_missing_parts_while_restoring_available_requests():
    state = items_state.State(
        queue=[_request(request_id=f"id{n:03d}") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )
    contents = items_state.encode_state(state)
    assert len(contents) > 2

    restored = items_state.decode_shards(contents[:1] + contents[2:])

    assert restored is not None
    assert restored.missing_parts == (1,)
    assert [request.id for request in restored.queue] != [request.id for request in state.queue]


def test_fits_is_true_at_the_shard_limit_and_false_past_it():
    requests = [_request(request_id=f"id{n:03d}") for n in range(200)]
    first_over_limit = next(
        count
        for count in range(len(requests) + 1)
        if len(items_state.encode_state(items_state.State(queue=requests[:count])))
        > items_state.MAX_SHARDS
    )
    at_limit = items_state.State(queue=requests[: first_over_limit - 1])
    past_limit = items_state.State(queue=requests[:first_over_limit])

    assert len(items_state.encode_state(at_limit)) == items_state.MAX_SHARDS
    assert items_state.fits(at_limit)
    assert not items_state.fits(past_limit)


def test_small_queue_uses_exactly_one_shard():
    contents = items_state.encode_state(items_state.State(queue=[_request()]))

    assert len(contents) == 1


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
